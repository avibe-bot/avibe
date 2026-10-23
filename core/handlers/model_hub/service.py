"""Model Hub aggregate service used by REST routes and backend injection."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Collection, Iterable, Literal, Mapping, NamedTuple, Optional, Protocol, cast

from sqlalchemy import func, literal_column, select

from config import paths
from modules.agents.catalog import NATIVE_CLI_BACKENDS
from config.v2_config import (
    CONFIG_LOCK,
    MODEL_HUB_BACKENDS,
    ModelHubAgentSupplyConfig,
    ModelHubBackendModelConfig,
    ModelHubConfig,
    is_model_hub_model_id,
    model_hub_fixed_menu_ids,
    ModelHubHostedToolConfig,
    ModelHubMenuConfig,
    ModelHubModelConfig,
    ModelHubRouteConfig,
    ModelHubRouteHopConfig,
    ModelHubSourceConfig,
    ModelHubSourceStateConfig,
    ModelHubSourceUsageConfig,
    V2Config,
    AGENT_CREDENTIAL_SECTIONS,
    config_recovery_refusal,
    normalize_storable_backend_model_text,
    normalize_model_hub_base_url,
    normalize_model_hub_vendor_id,
    model_hub_search_service,
    normalized_model_hub_override,
    validate_model_hub_protocol_vendor,
    validate_model_hub_source_client_nonce,
)
from core.agent_auth_service import BackendLoginInProgressError
from core.agent_core.ai.registry import sent_effort
from core.agent_core.messages import HostedCost
from core.os_errors import format_os_errno, local_error_detail
from core.services.settings import default_config
from storage.db import get_cached_sqlite_engine
from storage.models import MESSAGE_TURN_ID_SQL, agent_sessions, messages
from vibe.backend_model_catalog import (
    ImageModelSpec,
    PROTOCOL_REASONING_EFFORT_DEFAULTS,
    builtin_display_name,
    bundled_catalog_wire_efforts_by_model,
    bundled_client_presets_once,
    bundled_provider_overlay,
    hosted_tool_spec,
)
from vibe.i18n import t as i18n_t
from vibe.model_hub_runtime.client import RESPONSE_TOO_LARGE
from vibe.model_hub_runtime.api_key_vendors import (
    api_key_vendor_catalog,
    catalog_api_key_vendor_label,
    pinned_api_key_protocol,
    search_service,
)

from .adapter import (
    ENGINE_TRANSPORT_TIMEOUT_SECONDS,
    DiscoveredModel,
    EngineAdapter,
    EngineEnsureResult,
    EngineHealth,
    EngineStatus,
    InvokeCancelledError,
    InvokeHandle,
    OAuthFlowState,
    OAuthSubmissionRejectedError,
    OriginNotAllowedError,
    RawCallOutcome,
    RawOutcomeKind,
    RetainedMaterialDisposition,
    RuntimePlatformUnsupportedError,
    ObservationDiscovery,
    ObservationOutcome,
    MODEL_PROTOCOLS,
    SourceObservation,
    SearchCredentialCheck,
    SourceBinding,
    make_source_observation,
    validate_source_observation,
)
from .address_repair import (
    payload_carries_credential_address,
    repair_credential_addresses,
)
from .async_owner import await_owned_task, run_owned_in_thread
from .catalog_admission import (
    admissible_backend_model,
    backend_model_admission_error,
)
from .classification import (
    ResolutionDecision,
    classify_outcome,
    machine_codes_name_model,
    names_model_refusal,
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
)
from .capability_evidence import (
    CapabilityEvidenceStore,
    EvidenceFeature,
    EvidenceRecord,
    identity_digest,
    utc_text,
)
from .errors import ModelDiscoveryError
from .evidence_rules import LearnedEvidence
from .evidence_rules import learn as learn_from_rejection
from .evidence_rules import wire_effort
from .hosted_admission import PIN_UNAVAILABLE_CODE, HostedContent, admits, hosted_content
from .hosted_executor import (
    EXECUTOR_RESPONSE_MAX_BYTES,
    ExecutorCall,
    ExecutorCallRefused,
    ExecutorResponseRefused,
    ResponsesStreamReader,
    anthropic_result,
    call_timeout,
    executor_request,
    executor_streams,
    Failure,
    failed_record,
    failure,
    parse_call,
    provider_charges,
    responses_result,
)
from .hosted_images import (
    IMAGE_RESPONSE_MAX_BYTES,
    IMAGE_TIMEOUT_SECONDS,
    image_request,
    image_result,
    parse_image_call,
    unsupported_argument,
)
from .hosted_turn import IMAGE_TOOL, ExecutorSlot, TurnHostedTools
from . import hosted_tools as hosted_tools_module
from .search_services import ServiceCooldowns
from .search_chain import SearchChain
from .hosted_tools import (
    ExecutorCandidate,
    HostedEvidence,
    HostedToolSettingRefused,
    admitted_image,
    apply_hosted_tool_put,
    clear_request,
    engine_stamp,
    executor_candidate,
    executor_vendors,
    freeze_turn_tools,
    hop_hosted_tools,
    hosted_tools_status,
    image_models_of,
    refused_executors,
)
from .identifiers import (
    OPENCODE_PROVIDER_BY_NATIVE_PROTOCOL,
    canonical_model_id,
    normalized_model_id,
    opencode_menu_model_candidates,
    opencode_menu_model_id,
)
from .migration import (
    MigrationConflictError,
    MigrationCredentialsInvalidError,
    MigrationReauthorizationRequiredError,
    apply_native_migration,
    prepare_takeover_reauthentication,
    recover_native_migration,
    scan_native_configs,
)
from .migration_journal import NativeTakeoverJournal, TakeoverStateError
from .oauth import (
    NativeOAuthAdapter,
    NativeOAuthUnavailableError,
    OAuthAdapter,
    OAuthChannel,
    OAuthFlowBinding,
    OAuthFlowRegistry,
    UnavailableNativeOAuthAdapter,
)
from .pricing import (
    VALUE_WINDOW_DAYS,
    PriceTable,
    context_price_steps,
    hosted_response_cost,
    load_price_table,
    quota_values,
)
from .quota import QuotaSourceRef, SubscriptionQuotaCache, SubscriptionQuotaError
from .provenance import (
    REQUEST_NONFALLBACK_TURN_OUTCOME,
    BoundedProvenanceStore,
    ENGINE_DOWN_TURN_OUTCOME,
    ExactHopBlocker,
    HopOrigin,
    ServedHopHeaderTooLarge,
    TurnOutcomeProjectionInput,
    TurnUpstreamFailure,
    exact_hop_blockers,
    no_candidate_decision,
    produce_turn_outcome,
)
from .request import ModelHubRequest, without_opaque_history
from .reasoning_tiers import catalog_wire_efforts, rederive_persisted_tiers, resolve_reasoning_tiers
from .stream_wire import ProtocolSSEState, ProtocolUsageReport
from .resolver import (
    BackendName,
    ModelHubTurnResolution,
    SourceRecoveryAnnotation,
    allowed_origins,
    effective_model_route,
    inspect_exact_hop,
    matching_v1_model_id as _matching_v1_model_id,
    parse_model_hub_timestamp,
    resolve_model_hub_turn,
    source_after_cooldown_recovery,
    source_eligible_for_backend,
    source_model_retired,
    source_supports_passthrough,
    source_runnable,
)
from .revocations import CredentialRevocationJournal, PendingCredentialRevocation, RevocationOperation
from .retry import (
    RECOVERY_ADMISSION_MARGIN_SECONDS,
    RECOVERY_EXHAUSTED_CODE,
    RecoveryPolicy,
    RecoveryRequest,
    RETRY_DELAYS,
    source_identity,
)
from .usage import (
    USAGE_DEFAULT_WINDOW_DAYS,
    USAGE_WINDOW_KEYS,
    BoundedUsageLedger,
    ServiceCatalogEntry,
    SourceIdentity,
    UsageWriter,
    local_usage_day,
)

CONTRACT_VERSION = 20


def seeded_source_name(vendor: str) -> str:
    """The name a newly created Source of ``vendor`` starts life with.

    One owner for both create paths. A vendor id is a routing key, not a name,
    so the shipped catalog's label is the seed wherever it lists the vendor —
    otherwise a subscription reads as ``xai`` beside an api-key Source of the
    same vendor reading ``xAI``. Falls back to the id for a vendor the catalog
    does not list, which is the only name that channel has. The user owns the
    field from here on; this is the seed, not the display rule.
    """
    return catalog_api_key_vendor_label(vendor) or vendor


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


def _cached_models_dev_catalog() -> Mapping[str, Any]:
    from vibe.models_dev_catalog import load_models_dev_catalog_with_date

    return load_models_dev_catalog_with_date()[0]


def _with_text_only_models(
    bindings: list[SourceBinding],
) -> list[SourceBinding]:
    """Mark each bound model that its upstream's own models.dev entry declares text-only.

    Only an ``openai_chat`` Source on its vendor's official endpoint has such
    an entry: the vendor's mapped provider describes what that endpoint
    accepts. Read at sync rather than in ``_bindings``: the cached copy
    changes on its own, and a mutation compares the bindings it builds before
    and after itself. The sync never fetches, and an unreadable copy leaves
    every model undeclared, which is the engine's default.
    """

    from vibe.model_hub_runtime.api_key_vendors import official_models_dev_provider
    from vibe.models_dev_catalog import cached_models_dev_catalog, text_only_model_ids

    try:
        providers = {
            binding.source_id: official_models_dev_provider(binding.vendor, binding.base_url)
            for binding in bindings
            if binding.protocol == "openai_chat"
        }
        model_ids: dict[str, list[str]] = {}
        for binding in bindings:
            provider = providers.get(binding.source_id)
            if provider is not None:
                model_ids.setdefault(provider, []).extend((*binding.model_ids, *binding.route_model_ids))
        if not model_ids:
            return bindings
        catalog = cached_models_dev_catalog()
        text_only = {
            provider: text_only_model_ids(provider, ids, catalog) for provider, ids in model_ids.items()
        }
    except Exception as exc:  # noqa: BLE001 - optional metadata never fails a sync
        logger.info("Model Hub engine models have no models.dev modalities: %s", type(exc).__name__)
        return bindings
    return [
        replace(
            binding,
            text_only_model_ids=tuple(
                model
                for model in dict.fromkeys((*binding.model_ids, *binding.route_model_ids))
                if model in text_only.get(providers.get(binding.source_id) or "", ())
            ),
        )
        for binding in bindings
    ]


_MODELS_DEV_CANDIDATE_FIELDS = (
    "models_dev_id",
    "context_window",
    "max_output_tokens",
    "input_modalities",
    "output_modalities",
    "supports_tools",
    "supports_reasoning",
)


def _described_as_fit_for_agent_turns(match: Mapping[str, Any]) -> bool:
    """models.dev says the model takes text and tool calls and answers in text: the least an Agent's turn needs."""

    return (
        match.get("supports_tools") is True
        and "text" in (match.get("input_modalities") or ())
        and "text" in (match.get("output_modalities") or ())
    )


def _described_as_unfit_for_agent_turns(match: Mapping[str, Any]) -> bool:
    """models.dev says the model takes no tool calls, or takes or answers in something other than text."""

    if match.get("supports_tools") is False:
        return True
    return any(
        bool(modalities) and "text" not in modalities
        for modalities in (match.get("input_modalities") or (), match.get("output_modalities") or ())
    )


AGENT_CHAIN_CONTRACT_VERSION = 20
PROBE_RESULT_CONTRACT_VERSION = 20
_SOURCE_DISCOVERY_TIMEOUT_SECONDS = 15
_SOURCE_PROBE_TIMEOUT_SECONDS = 60
# Background inventory refresh. The first tick doubles as the startup delay, so
# a stale Source catches up shortly after boot without boot waiting on upstream.
_BACKGROUND_DISCOVERY_INTERVAL = timedelta(hours=6)
_BACKGROUND_DISCOVERY_JITTER = 0.2
_BACKGROUND_DISCOVERY_TICK = timedelta(minutes=5)
_BACKGROUND_DISCOVERY_RETRY_BASE = timedelta(minutes=15)
_BACKGROUND_DISCOVERY_RETRY_CAP = timedelta(hours=6)
# A viewer of Source inventory asks for listings at most this old.
_VIEWED_DISCOVERY_AGE = timedelta(minutes=30)
# A Source marked stale is listed again once its listing is this old.
_STALE_LISTING_AGE = timedelta(minutes=15)
# A credential waiting on the user cannot list models until the user acts.
_UNDISCOVERABLE_SOURCE_DETAILS = frozenset({
    "models.source.needs_action.oauth_expired",
    "models.source.needs_action.credential_revoked",
    "models.source.needs_action.account_banned",
})
_REORDER_ORDER_UNSET = object()
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

    if model.native_protocol not in OPENCODE_PROVIDER_BY_NATIVE_PROTOCOL:
        raise ValueError("OpenCode model native_protocol is invalid")
    identifier = model.id
    projected: dict[str, Any] = {
        "id": identifier,
        "name": model.display_name or identifier,
    }
    if model.context_window is not None or model.max_output_tokens is not None:
        # OpenCode requires both limits once either is present and validates the
        # whole config file on start, so one partial entry fails every launch.
        # It reads 0 as unknown: no overflow compaction for the context, and its
        # default cap for the output.
        projected["limit"] = {
            "context": model.context_window if model.context_window is not None else 0,
            "output": model.max_output_tokens if model.max_output_tokens is not None else 0,
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
    if model.supports_reasoning is False:
        projected["reasoning"] = False
    elif (
        model.supports_reasoning is True
        and model.native_protocol == "openai_responses"
    ):
        # Anthropic variants already express support. ``reasoning: true`` makes
        # that SDK add a budget-based option which overrides the selected effort.
        projected["reasoning"] = True
    variant_key = {
        "openai_responses": "reasoningEffort",
        "anthropic": "effort",
    }[model.native_protocol]
    variants = (
        {effort: {variant_key: effort} for effort in model.reasoning_efforts}
        if model.supports_reasoning is not False
        else {}
    )
    # Translate an explicitly declared Off variant, without inventing one or
    # changing the existing payloads of ordinary/custom effort values.
    if model.native_protocol == "anthropic" and "none" in variants:
        variants["none"] = {"thinking": {"type": "disabled"}}
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
        local_error_detail: str | None = None,
        origin: HopOrigin | None = None,
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
        self.local_error_detail = local_error_detail
        # Only surfaced upstream responses carry an admitted, immutable origin.
        # Local refusals/exhaustion must not inherit the last attempted hop.
        self.origin = origin


def turn_refusal(error: BaseException | None) -> TurnOutcomeProjectionInput | None:
    """The turn outcome a Model Hub error refuses its turn with, if it is one.

    Such an error states its own cause, so text matching on its message can
    only misread it, for example a model id that happens to say ``oauth``.
    """

    return error.turn_outcome if isinstance(error, ModelHubError) else None


class CredentialCleanupUnsettledError(ModelHubError):
    def __init__(self):
        super().__init__("engine_down", status=503)


class EngineUnavailableError(RuntimeError):
    pass


class ModelHubConfigStore(Protocol):
    def load(self) -> ModelHubConfig: ...

    def save(self, config: ModelHubConfig) -> None: ...

    def mutate(self, mutator: Callable[[ModelHubConfig], bool]) -> None: ...


def _file_identity(path: Path) -> tuple | None:
    """What changes whenever the config file is replaced or rewritten: its path, inode and size, and its times."""

    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


class V2ModelHubConfigStore:
    """Model Hub's config, read from the shared config file and served from a snapshot (#2568).

    Reading the file parses the whole config, which grows with Sources,
    inventories and routes, and Model Hub reads it on every request. So the
    store keeps the last parse and serves a copy of it while the file is
    unchanged: a read costs one ``stat`` and a copy, never a parse. Any change to
    the file (a write through this store, another writer in this process, the UI
    process or the CLI) replaces or rewrites it, which changes its identity, and
    the next read parses again. A write through this store keeps what it
    committed as the snapshot, inside its own lock, so the reads after it parse
    nothing.
    """

    _NATIVE_AUTH_FIELDS = {
        "claude": ("auth_mode", "api_key", "base_url", "auth_mode_set"),
        "codex": ("auth_mode", "api_key", "base_url", "oauth_relay_marker"),
    }

    def __init__(self) -> None:
        self._snapshot_lock = threading.Lock()
        self._snapshot: tuple[tuple, ModelHubConfig] | None = None

    def native_auth_snapshot(self, backends: tuple[str, ...]) -> dict[str, dict]:
        try:
            config = V2Config.load()
        except FileNotFoundError:
            return {}
        return {
            backend: {
                name: getattr(getattr(config.agents, backend), name)
                for name in self._NATIVE_AUTH_FIELDS[backend]
            }
            for backend in backends if backend in self._NATIVE_AUTH_FIELDS
        }

    def save_takeover(
        self,
        model_hub: ModelHubConfig,
        expected: dict[str, dict],
        desired: dict[str, dict],
        *,
        expected_hub: tuple[dict, ...],
    ) -> None:
        from config.v2_config import config_file_lock, config_write_transaction

        self.ensure_writable()
        path = paths.get_config_path()
        with config_file_lock(path):
            with config_write_transaction(path) as config:
                if config.model_hub.to_payload() not in expected_hub:
                    raise MigrationConflictError
                for backend, values in desired.items():
                    target = getattr(config.agents, backend)
                    actual = {name: getattr(target, name) for name in values}
                    if actual not in (expected.get(backend), values):
                        raise MigrationConflictError
                    for name, value in values.items():
                        setattr(target, name, value)
                config.model_hub = ModelHubConfig.from_payload(model_hub.to_payload())
            self._seed(path, config.model_hub)

    def load(self) -> ModelHubConfig:
        path = paths.get_config_path()
        identity = _file_identity(path)
        with self._snapshot_lock:
            snapshot = self._snapshot
        if snapshot is not None and identity is not None and snapshot[0] == identity:
            # Callers own what they load and may change it; the snapshot stays as the file holds it.
            return copy.deepcopy(snapshot[1])
        fresh = self._read(path)
        return default_config().model_hub if fresh is None else fresh[1]

    def _read(self, path: Path) -> tuple[tuple, ModelHubConfig] | None:
        """Parse the file and keep the parse, when the file did not change while it was read."""

        before = _file_identity(path)
        try:
            model_hub = V2Config.load().model_hub
        except FileNotFoundError:
            return None
        if before is not None and _file_identity(path) == before:
            with self._snapshot_lock:
                self._snapshot = (before, copy.deepcopy(model_hub))
        return (before or (), model_hub)

    def ensure_writable(self) -> None:
        try:
            config = V2Config.load()
        except FileNotFoundError:
            return
        # Hub writes also move Agent backend settings (native migration).
        if config_recovery_refusal(config, *AGENT_CREDENTIAL_SECTIONS) is not None:
            raise ModelHubError("config_recovery", status=409)

    def save(self, model_hub: ModelHubConfig) -> None:
        model_hub = ModelHubConfig.from_payload(model_hub.to_payload())
        self.ensure_writable()
        from config.v2_config import config_file_lock, update_config_fields

        path = paths.get_config_path()
        with config_file_lock(path):
            committed = update_config_fields(lambda cfg: setattr(cfg, "model_hub", model_hub), config_path=path)
            self._seed(path, committed.model_hub)

    def mutate(self, mutator: Callable[[ModelHubConfig], bool]) -> None:
        from config.v2_config import config_file_lock

        path = paths.get_config_path()
        with config_file_lock(path):
            config = V2Config.load()
            if mutator(config.model_hub):
                config.save()
                self._seed(path, config.model_hub)

    def _seed(self, path: Path, model_hub: ModelHubConfig) -> None:
        """Keep what a write just committed as the snapshot, without reading the file back.

        Called inside the write's config lock, so no other writer can have
        replaced the file since: its identity now names exactly this content. The
        section is settled the way a load settles it (the one repairing door).
        """

        identity = _file_identity(path)
        if identity is not None:
            settled = ModelHubConfig.from_payload(model_hub.to_payload(), repairing=True)
            with self._snapshot_lock:
                self._snapshot = (identity, settled)

class UnavailableEngineAdapter:
    """Explicit fail-closed adapter for isolated callers and tests."""

    async def install(self) -> EngineStatus:
        return await self.status()

    async def recover_installation(self) -> EngineStatus:
        return await self.status()

    async def ensure_installed(
        self,
        *,
        force: bool = False,
        offline: bool = False,
    ) -> EngineEnsureResult:
        return EngineEnsureResult(status=await self.status(), changed=False)

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

    async def provision_credential(
        self, vendor: str, protocol: str, secret: str, base_url: str | None,
        *, auth_scheme: str | None = None, on_reserved: Callable[[str], None] | None = None,
    ) -> str:
        raise EngineUnavailableError

    async def provision_oauth_credential(
        self,
        source_id: str,
        vendor: str,
        material: Mapping[str, object],
        *, on_reserved: Callable[[str], None] | None = None,
    ) -> str:
        raise EngineUnavailableError

    async def activate_oauth_credential(self, credential_ref: str) -> None:
        raise EngineUnavailableError

    async def validate_oauth_credential(self, credential_ref: str) -> None:
        raise EngineUnavailableError

    async def matches_api_key_credential(
        self, credential_ref: str, vendor: str, protocol: str,
        secret: str, base_url: str | None,
        *, auth_scheme: str | None = None,
    ) -> bool:
        raise EngineUnavailableError

    async def credential_auth_scheme(self, credential_ref: str) -> str | None:
        raise EngineUnavailableError

    def subscription_account_label(self, source_id: str, vendor: str, credential_ref: str) -> str | None:
        return None

    async def subscription_quota(self, source_id: str, vendor: str, credential_ref: str) -> dict[str, Any]:
        raise SubscriptionQuotaError("unavailable")

    async def provision_transient_credential(
        self, vendor: str, secret: str, base_url: str | None,
        *, auth_scheme: str | None = None, on_reserved: Callable[[str], None] | None = None,
    ) -> str:
        raise EngineUnavailableError

    async def revoke_credential(self, credential_ref: str) -> None:
        raise EngineUnavailableError

    async def revoke_api_key_credential(self, credential_ref: str) -> None:
        raise EngineUnavailableError

    async def revoke_search_credential(self, credential_ref: str) -> None:
        raise EngineUnavailableError

    async def sync_sources(self, bindings) -> None:
        raise EngineUnavailableError

    async def discover_models(
        self,
        vendor: str,
        protocol: str,
        base_url: str | None,
        credential_ref: str,
        *,
        start_engine: bool = True,
    ):
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
        *,
        on_admitted: Callable[[], None] | None = None,
    ) -> InvokeHandle:
        raise EngineUnavailableError


class _InvocationPlanChanged(Exception):
    """No transport was admitted; recompute the remaining effective route."""


class _InvocationUnsupported(_InvocationPlanChanged):
    """A request-specific transport limitation, not a Source health failure."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _pinned_hop(request: Any, backend: str) -> tuple[str, str | None] | None:
    """The exact (Source, model) a Vibey request is pinned to (``x-avibe-pin-source``), if any."""

    return getattr(request, "pinned_hop", None) if backend == "vibey" else None


def _pin_unavailable(learned: LearnedEvidence | None = None, source_id: str = "") -> ModelHubError:
    """The pinned hop cannot serve this request before output (``vibey-hosted-tools.md`` §3.5).

    ``learned`` is the hosted feature a narrow rule just refused on that hop;
    only then does the answer carry a ``reason``.
    """

    reason = (
        {"kind": "hosted_unsupported", "tool": learned.feature.name, "aspect": learned.feature.aspect,
         "source_id": source_id}
        if learned is not None else None
    )
    return ModelHubError(PIN_UNAVAILABLE_CODE, status=409, data={"reason": reason} if reason else None)


class UserTestCall(NamedTuple):
    """One call a user asked for, as it settled (``ModelHubService._user_test_call``)."""

    source: ModelHubSourceConfig
    outcome: RawCallOutcome
    elapsed_ms: int


def _call_usage(outcome: RawCallOutcome | None, observed: ProtocolSSEState | None) -> ProtocolUsageReport | None:
    """The tokens one upstream call reported: its outcome's, else what was observed on the wire."""

    usage = outcome.usage if outcome is not None else None
    return observed.usage if usage is None and observed is not None else usage


def _executor_failure(outcome: RawCallOutcome | None, say: Callable[..., str], *, refused: str | None) -> Failure | None:
    """How a proxied hosted call failed, in the user's language and by its code; None when it succeeded.

    ``refused`` names a bound the response itself broke (size, nesting).
    """

    if refused is not None:
        return failure(say, refused)
    if outcome is None or outcome.kind is RawOutcomeKind.SUCCESS:
        return None
    if outcome.error_code == RESPONSE_TOO_LARGE:
        # The engine client cut an answer past its bound while it downloaded.
        return failure(say, "modelHub.hostedTools.tooLarge")
    if outcome.kind is RawOutcomeKind.TIMEOUT:
        return failure(say, "modelHub.hostedTools.timedOut")
    if outcome.kind is RawOutcomeKind.NETWORK_ERROR:
        return failure(say, "modelHub.hostedTools.unreachable")
    if outcome.kind is RawOutcomeKind.PROTOCOL_ERROR:
        return failure(say, "modelHub.hostedTools.unreadable")
    # The provider's own machine code and its redacted, bounded detail stay verbatim.
    detail = " ".join(
        part for part in (
            f"HTTP {outcome.http_status}" if outcome.http_status is not None else None,
            outcome.error_code,
        ) if part
    )
    if outcome.upstream_detail:
        detail = f"{detail}: {outcome.upstream_detail}" if detail else outcome.upstream_detail
    if classify_outcome(replace(outcome, stream_started=False)).reason == "server_error":
        # An overload or server error is transient: the provider did not refuse the call.
        return failure(say, "modelHub.hostedTools.providerUnavailable", detail=detail or outcome.kind.value)
    return failure(say, "modelHub.hostedTools.providerRefused", detail=detail or outcome.kind.value)


def _cost_payload(cost: HostedCost) -> dict[str, Any]:
    payload = {"basis": cost.basis} if cost.usd is None else {"basis": cost.basis, "usd": cost.usd}
    if cost.estimate is not None:
        payload["estimate"] = cost.estimate
    return payload


@dataclass(frozen=True)
class _HostedUpstreamCall:
    """How one upstream call a hosted tool made ended (``ModelHubService._hosted_upstream_call``)."""

    # The Source as the call's final admission saw it: what the call reports (billing, cost, name) follows it.
    source: ModelHubSourceConfig
    outcome: RawCallOutcome | None
    # A bound the response broke, as a failure key to show.
    refused: str | None
    timed_out: bool
    usage: ProtocolUsageReport | None
    # Whether the provider could have charged for it: it reported tokens or produced a response.
    billable: bool


@dataclass(frozen=True)
class _ExecutorAttempt:
    """One executor candidate a search chain called, as it settled (``vibey-hosted-tools.md`` §6)."""

    source: ModelHubSourceConfig
    model_id: str
    record: dict[str, Any]
    cost: Optional[HostedCost]
    # Whether the provider could have charged for it: it reported tokens or produced a response.
    billable: bool
    # Whether its failure passes the search to the next candidate.
    moves_on: bool
    kind: str = "model"
    superseded: bool = False

    @property
    def billing(self) -> str:
        return "metered" if self.kind == "service" else self.source.billing

    def execution(self) -> dict[str, Any]:
        execution: dict[str, Any] = {
            "mode": "proxy",
            "executor_origin": {
                "provider": self.source.vendor,
                "api": "search_api" if self.kind == "service" else self.source.protocol,
                "model": self.model_id,
            },
            "executor_source_id": self.source.id,
            "executor_billing": self.billing,
        }
        if self.cost is not None:
            execution["cost"] = _cost_payload(self.cost)
        return execution

    def failed_attempt(self) -> dict[str, Any]:
        attempt: dict[str, Any] = {"source_id": self.source.id, "billing": self.billing}
        if self.cost is not None:
            attempt["cost"] = _cost_payload(self.cost)
        return attempt


def _sent_effort(backend: str, source: ModelHubSourceConfig, model_id: str, effort: str) -> Optional[str]:
    """The effort token ``backend`` sends a hop for the menu value ``effort``: the key its evidence is held under.

    Vibey's adapter for the hop's protocol says it (``registry.sent_effort``),
    so the menu and the wire cannot drift; the native CLIs send the value as
    the menu names it.
    """

    if backend == "vibey":
        return sent_effort(source.protocol, model_id, effort)
    return effort


def _google_admission_skip_reason(
    frontend_protocol: str | None,
    source_protocol: str,
    model_id: str,
    stream: bool,
) -> str | None:
    """Pinned-engine facts; callers explicitly own where refusal applies."""
    if frontend_protocol != "google":
        return None
    # Pinned CPA splits the decoded action on every colon; escaping cannot
    # preserve a model colon through that parser.
    if ":" in model_id:
        return "google_model_path_unsupported"
    # CPA c404af96 omits finishReason on its Responses -> Gemini
    # response.completed conversion. Usage/EOF cannot supply that terminal.
    if stream and source_protocol == "openai_responses":
        return "google_stream_responses_unsupported"
    return None


class _RecoveryWindowClosed(Exception):
    """The request may finish its admitted inference, but cannot start another."""


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
    verification_pending: Optional[str] = None
    origin: HopOrigin | None = None
    # Vibey: the catalog tools the request declared natively, which a served
    # response may prove ran (``vibey-hosted-tools.md`` §3.2).
    hosted_calls: frozenset[str] = frozenset()


@dataclass(frozen=True)
class HandleSettlement:
    outcome: RawCallOutcome | None
    decision: ResolutionDecision | None
    turn_outcome: TurnOutcomeProjectionInput | None


HandleTerminationOrigin = Literal["downstream_cancel", "upstream_terminal"]


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
        Optional[HopOrigin],
    ],
    None,
]


@dataclass(frozen=True)
class _DiscoveryRetry:
    identity: tuple
    failed_at: datetime
    delay: timedelta


def _background_discovery_period(source_id: str) -> timedelta:
    """A stable per-Source period within the jitter window around the interval.

    Sources listed together then drift apart instead of refreshing in lockstep,
    and a restart keeps each Source's phase.
    """

    fraction = int.from_bytes(hashlib.sha256(source_id.encode()).digest()[:8], "big") / 2**64
    return _BACKGROUND_DISCOVERY_INTERVAL * (
        1 - _BACKGROUND_DISCOVERY_JITTER + 2 * _BACKGROUND_DISCOVERY_JITTER * fraction
    )


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
        if model.native_protocol not in OPENCODE_PROVIDER_BY_NATIVE_PROTOCOL:
            raise ValueError("OpenCode model native_protocol is invalid")
        public_model = project_opencode_public_model(model)
        public_model["native_protocol"] = model.native_protocol
        projected[model.id] = public_model
    return projected


def load_opencode_public_models(
    config: ModelHubConfig | None = None,
) -> dict[str, dict[str, Any]]:
    """Return the credential-free OpenCode projection for local callers."""

    return _project_opencode_public_models(
        config if config is not None else V2ModelHubConfigStore().load(),
        now=_utc_now(),
    )


def _source_id() -> str:
    return f"src_{uuid.uuid4().hex[:12]}"


def _mask_credential(value: str) -> str:
    """Create the one-way display mask frozen by the source contract."""
    normalized = value.strip()
    if len(normalized) <= 4:
        return "…" + ("•" * len(normalized))
    prefix_length = min(7, len(normalized) - 5)
    return f"{normalized[:prefix_length]}…{normalized[-4:]}"


def search_capability_for_source(source: ModelHubSourceConfig) -> dict[str, Any] | None:
    """Return the bundled search service when this Source is search-capable."""
    return model_hub_search_service(source)


def _search_service_for_source(source: ModelHubSourceConfig) -> dict[str, Any] | None:
    return search_capability_for_source(source)


def _require_search_service_for_source(source: ModelHubSourceConfig) -> dict[str, Any]:
    """A retained Source may be readable without still supporting mutations."""
    service = _search_service_for_source(source)
    if service is None:
        raise ModelHubError("discovery_failed")
    return service


def _maintain_search_service_order(config: ModelHubConfig, source_id: str, *, add: bool) -> None:
    setting = config.hosted_tools.get("web_search")
    existing = tuple(setting.service_order or ()) if setting is not None else ()
    if add:
        if source_id in existing:
            return
        order = (*existing, source_id)
    else:
        order = tuple(item for item in existing if item != source_id)
    if setting is None:
        if not add:
            return
        setting = ModelHubHostedToolConfig()
    config.hosted_tools["web_search"] = replace(setting, service_order=order)


def _search_check_outcome(value: object) -> Literal["pass", "balance", "limit", "invalid", "unknown"]:
    if isinstance(value, SearchCredentialCheck):
        return value.outcome
    raw = getattr(value, "outcome", value)
    return raw if raw in {"pass", "balance", "limit", "invalid", "unknown"} else "unknown"


def _validated_base_url(value: object) -> Optional[str]:
    try:
        return normalize_model_hub_base_url(value)
    except (TypeError, ValueError):
        raise ModelHubError("discovery_failed") from None


def _validated_source_name(value: object) -> str:
    """A Source name as the Web UI reads it: trimmed, present, and never a pasted credential.

    Admission owns the trim, not the config parser: a name an earlier release
    stored with edge or only whitespace must keep loading.
    """
    name = value.strip() if isinstance(value, str) else ""
    if not name or len(name) > 64 or contains_credential_material(name):
        raise ModelHubError("discovery_failed")
    return name


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
    *,
    rollback_operation: RevocationOperation | None = None,
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
            operation=rollback_operation,
        )
        raise cancelled


async def _provision_transient_credential_with_cancellation_ownership(
    service: "ModelHubService",
    vendor: str,
    key: str,
    base_url: str | None,
    *, auth_scheme: str | None = None, on_reserved: Callable[[str], None] | None = None,
) -> str:
    options: dict[str, Any] = {"on_reserved": on_reserved} if on_reserved is not None else {}
    if auth_scheme is not None:
        options["auth_scheme"] = auth_scheme
    return await _acquire_credential_ref_with_cancellation_ownership(
        service,
        service.adapter.provision_transient_credential(vendor, key, base_url, **options),
        "observation",
    )


async def _rollback_credential_before_settling(
    service: "ModelHubService",
    source_id: str,
    credential_ref: str,
    *,
    operation: RevocationOperation | None = None,
) -> None:
    """Finish transient cleanup before propagating a cancellation."""

    rollback_task = asyncio.create_task(
        _require_credential_cleanup(
            service,
            source_id,
            credential_ref,
            operation=operation,
        )
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
    *,
    operation: RevocationOperation | None = None,
) -> None:
    """Refuse to settle when cleanup is neither complete nor durable."""

    if not await service._rollback_credential(
        source_id,
        credential_ref,
        operation=operation,
    ):
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


# Guard refusals that mean work a switch interrupted on the selected backends
# is still stopping. Every other refusal, such as a CLI running outside Avibe
# or an ordinary restart draining live work, stays the generic busy code.
_RUNTIME_STOPPING_REASONS = frozenset({"native_runtime_busy", "migration_teardown_in_progress"})


def _log_native_refusal(operation: str, error: Any) -> None:
    # The client sees only a closed busy/blocked code. Keep the credential-free
    # NativeMigrationBlockedError reason, backends and pids in the log instead.
    logger.warning(
        "%s refused: %s (backends=%s, pids=%s)",
        operation,
        error.reason,
        ",".join(error.backends),
        ",".join(str(pid) for pid in error.pids),
    )


def _binding(source: ModelHubSourceConfig) -> SourceBinding:
    if not source.credential_ref:
        raise ModelHubError("engine_down", status=503)
    source_origins = allowed_origins(source)
    if source.kind == "subscription" and not source_origins:
        raise ModelHubError("mode_switch_blocked", status=409)
    active_models = [model for model in source.models if not model.retired]
    return SourceBinding(
        source_id=source.id,
        vendor=source.vendor,
        protocol=source.protocol,
        base_url=source.base_url,
        credential_ref=source.credential_ref,
        allowed_origins=source_origins,
        model_ids=tuple(model.id for model in active_models),
        model_reasoning_efforts=tuple(
            (model.id, tuple(model.reasoning_efforts)) for model in active_models
        ),
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
    if flow.state == "failed" and flow.error_detail:
        payload["error_detail"] = flow.error_detail
    if getattr(flow, "application_failed", False) is True:
        payload["application_failed"] = True
    return payload


def _runtime_payload(status: EngineStatus, *, enabled: bool) -> dict:
    # Import lazily to avoid the runtime adapter's dependency back on this service module.
    from vibe.model_hub_runtime.installer import EngineRuntimeManager

    manager = EngineRuntimeManager()
    return {
        "contract_version": 20,
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


async def ensure_runtime_dependency(
    adapter: EngineAdapter,
    *,
    force: bool = False,
    offline: bool = False,
) -> EngineEnsureResult:
    """Converge the pinned engine through a controller-owned adapter."""

    ensure = getattr(adapter, "ensure_installed", None)
    if not callable(ensure):
        raise ModelHubError("engine_down", status=503)
    try:
        return await ensure(force=force, offline=offline)
    except RuntimePlatformUnsupportedError:
        raise ModelHubError("runtime_platform_unsupported", status=422) from None
    except EngineUnavailableError as exc:
        reason = getattr(exc, "reason", None)
        raise ModelHubError(
            "engine_down",
            status=503,
            data={"reason": reason} if isinstance(reason, str) and reason else None,
            local_error_detail=local_error_detail(exc),
        ) from None
    except ModelHubError:
        raise
    except Exception as exc:
        reason = getattr(exc, "reason", None)
        safe_reason = (
            reason
            if isinstance(reason, str) and reason.startswith("model_hub_engine_")
            else None
        )
        raise ModelHubError(
            "engine_down",
            status=503,
            data={"reason": safe_reason} if safe_reason else None,
            local_error_detail=local_error_detail(exc),
        ) from None


def runtime_dependency_payload(
    result: EngineEnsureResult,
    *,
    enabled: bool,
) -> dict:
    payload = _runtime_payload(result.status, enabled=enabled)
    payload["changed"] = result.changed
    return payload



def _drop_discovered_models(source: ModelHubSourceConfig, model_ids: Iterable[str]) -> bool:
    """Remove these listed discovered rows; whether one was there.

    A manual row is the user's, and a retired row is the tombstone that keeps
    a model the user removed from being listed again, so both stay.
    """

    dropping = set(model_ids)
    kept = [
        model for model in source.models
        if not (model.id in dropping and model.provenance == "discovered" and not model.retired)
    ]
    dropped = len(kept) != len(source.models)
    source.models = kept
    return dropped

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
        migration_home: Optional[Path] = None,
        migration_project_roots: Callable[[], tuple[Path, ...]] | None = None,
        migration_guard: Any = None,
        migration_reconcile_auth: Callable[[dict[str, dict[str, Any]]], None] | None = None,
        migration_journal: NativeTakeoverJournal | None = None,
        requested_model_override: Optional[Callable[[BackendName], Optional[str]]] = None,
        selected_agent_override: Optional[Callable[[BackendName], Optional[str]]] = None,
        named_agents_override: Optional[
            Callable[[BackendName], list[tuple[str, Optional[str]]]]
        ] = None,
        builtin_agent_models_override: Optional[
            Callable[[], list[tuple[BackendName, str]]]
        ] = None,
        cli_present_override: Optional[Callable[[BackendName], bool]] = None,
        cli_presence_refresh: Optional[
            Callable[[bool, tuple[BackendName, ...] | None], None]
        ] = None,
        backend_catalog_changed: Optional[
            Callable[[BackendName], Awaitable[None]]
        ] = None,
        repair_model_selections: Optional[
            Callable[[frozenset[str]], Awaitable[int]]
        ] = None,
        now: Callable[[], datetime] = _utc_now,
        recovery: RecoveryPolicy | None = None,
        price_table: Callable[[], PriceTable] | None = None,
        models_dev_catalog: Callable[[], Mapping[str, Any]] | None = None,
        capability_evidence: CapabilityEvidenceStore | None = None,
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
        # Read per report: the override file is hand-edited and the catalog refreshes itself.
        self.price_table: Callable[[], PriceTable] = price_table or (
            lambda: load_price_table(paths.get_state_dir())
        )
        # Built here, at controller startup before the loop serves, so the writer's
        # first price-table read never runs on the loop and every call is tiered.
        self.usage_writer = UsageWriter(self.usage, price_table=self.price_table)
        self.native_oauth_adapter = native_oauth_adapter or UnavailableNativeOAuthAdapter()
        self.oauth_flows = oauth_flows or OAuthFlowRegistry(
            paths.get_state_dir() / "model_hub_oauth_flows.json",
            now=now,
        )
        self.revocations = revocations or CredentialRevocationJournal(
            paths.get_state_dir() / "model_hub_pending_revocations.json"
        )
        self.migration_home = migration_home
        self.migration_project_roots = migration_project_roots or (lambda: ())
        self.migration_journal = migration_journal or NativeTakeoverJournal(
            events.path.parent / "native-takeover" / "current.json"
        )
        # Beside the other state documents, so it follows wherever they live.
        self.capability_evidence = capability_evidence or CapabilityEvidenceStore(
            events.path.parent / "model_hub_capability_evidence.json"
        )
        # Bundled state that never changes while this process runs, resolved once
        # here at startup so that no request path reads a file on the loop.
        self.engine_stamp = engine_stamp()
        from vibe import __version__ as avibe_release

        self.avibe_release = avibe_release
        bundled_provider_overlay()
        bundled_client_presets_once()
        # A custom Source's priors read its vendors' catalog protocols (``prior_vendors``).
        api_key_vendor_catalog()
        self.migration_blocked_backends: set[str] = set()
        self.migration_guard = migration_guard or self._unavailable_migration_guard
        self.migration_reconcile_auth = migration_reconcile_auth
        self._migration_lock = asyncio.Lock()
        self._migration_task: asyncio.Task | None = None
        self._migration_item_ids: tuple[tuple[str, ...], bool] | None = None
        self.requested_model_override = requested_model_override
        self.selected_agent_override = selected_agent_override
        self.named_agents_override = named_agents_override
        # The built-in Agents' models for Vibey's starting supply; the
        # controller owns those Agent rows.
        self.builtin_agent_models_override = builtin_agent_models_override
        self.cli_present_override = cli_present_override
        self.cli_presence_refresh = cli_presence_refresh
        self.backend_catalog_changed = backend_catalog_changed
        self.repair_model_selections = repair_model_selections
        self.now = now
        self.recovery = recovery or RecoveryPolicy(now=lambda: self.now())
        self.native_source_ready: Callable[[BackendName, ModelHubSourceConfig], bool] = (
            lambda _backend, _source: True
        )
        self._mutation_lock = asyncio.Lock()
        self._oauth_start_tasks: dict[
            tuple[str, str, OAuthChannel], asyncio.Task[dict]
        ] = {}
        self._source_create_nonces: set[str] = set()
        self._search_reenable_trials: set[str] = set()
        self._search_reenable_trials_inflight: set[str] = set()
        self._search_trial_epochs: dict[str, int] = {}
        self._search_trial_identities: dict[str, tuple] = {}
        self._search_reenable_checks: dict[str, asyncio.Task] = {}
        self._service_cooldowns = ServiceCooldowns()
        self._next_settlement_generation = PRE_ATTEMPT_SETTLEMENT_GENERATION
        self._latest_source_attempt_generation: dict[str, int] = {}
        self._source_attempt_identities: dict[str, tuple | None] = {}
        self._engine_synced = False
        self._engine_preparation_failed = False
        self._runtime_install_reconcile_lock = asyncio.Lock()
        self._runtime_install_reconciled = False
        self._runtime_lifecycle_lock = asyncio.Lock()
        self._runtime_resume_task: asyncio.Task[None] | None = None
        self._background_discovery_task: asyncio.Task[None] | None = None
        self._background_discovery_stop: asyncio.Event | None = None
        # Process-local schedule state: an unchanged listing persists nothing,
        # and a restart rediscovers stale Sources one tick after startup.
        self._background_discovery_checked: dict[str, datetime] = {}
        self._background_discovery_retries: dict[str, _DiscoveryRetry] = {}
        self._background_discovery_caught_up = False
        self._background_discovery_inflight: set[str] = set()
        self._viewed_discovery_task: asyncio.Task[None] | None = None
        # Per Source, the discovered rows it refused as models it does not serve,
        # filed under the identity (key, base URL, protocol) that refused them.
        self._stale_listings: dict[str, dict[tuple, frozenset[str]]] = {}
        self._builtin_snapshot_generations: dict[BackendName, str] = {}
        self._builtin_snapshot_cache: dict[BackendName, list[dict[str, Any]]] = {}
        self._pending_builtin_catalog_refresh: set[BackendName] = set()
        self.quota = SubscriptionQuotaCache(self._fetch_subscription_quota, now=lambda: self.now())
        # A cached copy only: the picker read must not wait on the network.
        self.models_dev_catalog: Callable[[], Mapping[str, Any]] = models_dev_catalog or (
            lambda: _cached_models_dev_catalog()
        )

    @staticmethod
    @asynccontextmanager
    async def _unavailable_migration_guard(backends, *, external_processes=True):
        # A stand-alone UI service cannot retire Controller-owned credentials.
        # Tests explicitly inject a fixture-only guard; production is wired by
        # the Controller's shared backend lifecycle coordinator.
        raise ModelHubError("mode_switch_blocked", status=409)
        yield  # pragma: no cover - required async context-manager shape

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
                return self._menu_model_for_selection(agent, requested_model)
        return ""

    @staticmethod
    def _menu_model_for_selection(
        agent: ModelHubAgentSupplyConfig,
        selected: str,
    ) -> str:
        """Name the menu row an Agent's model selection runs on in Gateway mode.

        Only OpenCode spells a Direct selection differently from its Gateway
        menu id; the turn path applies the same rule to its overlay snapshot.
        Direct mode runs the selection as it is spelled.
        """

        if agent.backend != "opencode" or agent.mode != "hub":
            return selected
        return opencode_menu_model_id(selected, {model.id for model in agent.models})

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
        overrides = [model_id for model_id, route in agent.routes.items() if normalized_model_hub_override(route) is not None]
        for model_id in [*primary, requested_model, *overrides]:
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

    async def _settle_pending_native_creates(self, vendor: str) -> None:
        """Keep the native slot claimed until a finished create is committed.

        The CLI login releases its credential lease as soon as it finishes, but
        its Source is committed only by a later status read. In between, the
        slot looks empty to both the singleton check and CLI custody, so another
        start would be admitted and overwrite the credential that login just
        wrote. Commit a finished create first, so the singleton check sees it,
        and refuse while one is still in progress.
        """

        for flow_id, binding in self.oauth_flows.pending_creates(vendor, "native_cli"):
            try:
                flow = await self._oauth_status(flow_id, "native_cli")
                self._raise_if_flow_expired(flow_id, flow)
            except ModelHubError as error:
                # Both reads forget a flow that can no longer finish.
                if error.code in {"flow_not_found", "flow_expired"}:
                    continue
                raise
            if flow.state == "success":
                await self._materialize_completed_oauth(flow_id, binding, flow)
            elif flow.state not in {"failed", "cancelled"}:
                raise ModelHubError(
                    "native_login_in_progress",
                    status=409,
                    detail="modelHub.errors.native_login_in_progress",
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
        except EngineUnavailableError as exc:
            raise ModelHubError("engine_down", status=503, local_error_detail=local_error_detail(exc)) from None
        except NativeOAuthUnavailableError:
            raise ModelHubError("engine_down", status=503) from None
        except ModelHubError:
            raise
        except Exception as exc:
            # Engine failures may carry upstream context. Never expose or log it.
            raise ModelHubError("engine_down", status=503, local_error_detail=local_error_detail(exc)) from None

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
        except OAuthSubmissionRejectedError as error:
            raise ModelHubError(
                "submission_rejected",
                status=422,
                detail=(
                    "modelHub.errors.submission_rejected_other_attempt"
                    if error.reason == "other_attempt"
                    else None
                ),
            ) from None
        except ModelHubError:
            raise
        except Exception as error:
            from core.backend_restart import NativeMigrationBlockedError

            # A native writer refusal is an answered state, not an engine
            # outage: reporting it as engine_down sends the user retrying a
            # start that the same refusal will block again.
            if isinstance(error, NativeMigrationBlockedError):
                code = (
                    error.reason
                    if error.reason in {"config_recovery", "migration_recovery_pending"}
                    else "migration_native_busy"
                )
                raise ModelHubError(code, status=409) from None
            raise ModelHubError("engine_down", status=503) from None

    def _bindings(self, config: ModelHubConfig) -> list[SourceBinding]:
        route_targets: dict[str, set[str]] = {}
        sources = {source.id: source for source in config.sources}
        for backend, agent in config.agents.items():
            for model_id in self._agent_model_ids(agent, ""):
                for hop in effective_model_route(config, cast(BackendName, backend), model_id).hops:
                    source = sources.get(hop.source_id)
                    if source is not None and source_supports_passthrough(source) and not source_model_retired(source, hop.model_id):
                        route_targets.setdefault(source.id, set()).add(hop.model_id)
        return [
            replace(_binding(source), route_model_ids=tuple(sorted(route_targets.get(source.id, ()))))
            for source in config.sources
            if source.supply_channel == "hub"
            and (source_supports_passthrough(source) or any(not model.retired for model in source.models))
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

    def _reconcile_native_auth(self, backends: tuple[str, ...]) -> None:
        """Mirror the actual persisted direction while takeover still owns admission."""
        reader = getattr(self.store, "native_auth_snapshot", None)
        snapshot = reader(backends) if callable(reader) else {}
        if self.migration_reconcile_auth is not None:
            self.migration_reconcile_auth(snapshot)

    async def _save_config(self, config: ModelHubConfig) -> ModelHubConfig:
        """Persist the Model Hub section; the one door every config write goes through.

        The file work (the migration journal check, the cross-process config
        lock, the rewrite of the whole config file) runs on a worker thread, so
        the controller loop never waits on another process's lock or a slow
        disk. Callers hold ``_mutation_lock`` from the load they changed to this
        save, so writes stay serialized. The recovery reconcile and its waiters'
        wake-up are loop state and stay on the loop.
        """

        worker = asyncio.create_task(asyncio.to_thread(self._persist_config, config.to_payload()))
        try:
            canonical = await asyncio.shield(worker)
        except asyncio.CancelledError as cancelled:
            # The write is underway in its thread and will land. Hold the caller, and the mutation lock it holds,
            # until it has, and publish it: otherwise a later write could start from the older snapshot and be
            # overwritten when this one finishes.
            try:
                canonical = await await_owned_task(worker)
            except BaseException:
                raise cancelled from None
            self._publish_saved(canonical)
            raise cancelled
        return self._publish_saved(canonical)

    def _publish_saved(self, canonical: ModelHubConfig) -> ModelHubConfig:
        """The loop-side half of a save: recovery follows the saved Sources and its waiters wake."""

        self.recovery.reconcile(canonical)
        self.recovery.notify()
        self._service_cooldowns.reconcile(canonical)
        live = {source.id: source_identity(source) for source in canonical.sources}
        for sid, identity in tuple(self._search_trial_identities.items()):
            if live.get(sid) != identity:
                self._drop_search_reenable_trial(sid)
        return canonical

    def _persist_config(self, payload: dict) -> ModelHubConfig:
        """``_save_config``'s blocking half, run off the loop: canonicalize and write."""

        canonical = ModelHubConfig.from_payload(payload)
        try:
            pending = self.migration_journal.load()
        except (TakeoverStateError, OSError):
            raise ModelHubError("migration_item_conflict", status=409) from None
        reverse = pending is not None and pending["phase"] == "reverting"
        if pending is not None and canonical.to_payload() not in (
            (pending["previous"],) if reverse else (
                pending["updated"], (pending.get("terminal") or {}).get("config"),
            )
        ):
            # The write-ahead decision must not overwrite intervening route
            # edits on recovery. Finish/revert that decision before new writes.
            raise ModelHubError("migration_item_conflict", status=409)
        if pending is not None and isinstance(self.store, V2ModelHubConfigStore):
            native_before = pending.get("native_before", {})
            self.store.save_takeover(
                canonical,
                pending.get("native_after", {}) if reverse else native_before,
                native_before if reverse else pending.get("native_after", {}),
                expected_hub=tuple(value for value in (
                    pending["previous"], pending["updated"],
                    (pending.get("terminal") or {}).get("config"),
                ) if value is not None),
            )
        else:
            self.store.save(canonical)
        return canonical

    async def _save_projection_neutral(
        self,
        previous: ModelHubConfig,
        updated: ModelHubConfig,
    ) -> ModelHubConfig:
        """Persist a synchronous mutation proven not to affect engine bindings."""

        if self._bindings(previous) != self._bindings(updated):
            raise AssertionError("projection-neutral mutation changed engine bindings")
        return await self._save_config(updated)

    def _save_projection_neutral_blocking(self, previous: ModelHubConfig, updated: ModelHubConfig) -> ModelHubConfig:
        """``_save_projection_neutral`` for a synchronous hook; it blocks the loop for the write.

        Only native reauth's irreversible-start hook uses it: that hook's contract (``AgentAuthService``'s
        ``on_irreversible_start``) is synchronous, and it writes once per user-started reauth. Everything else
        saves through ``_save_config``.
        """

        if self._bindings(previous) != self._bindings(updated):
            raise AssertionError("projection-neutral mutation changed engine bindings")
        return self._publish_saved(self._persist_config(updated.to_payload()))

    async def _save_runtime_config(
        self,
        previous: ModelHubConfig,
        updated: ModelHubConfig,
    ) -> bool:
        """Best-effort persistence for runtime state during config recovery."""

        try:
            await self._save_projection_neutral(previous, updated)
        except ValueError as exc:
            if "recovery warnings" not in str(exc):
                raise
            logger.warning("Skipped Model Hub runtime-state persistence during config recovery")
            return False
        return True

    def search_reenable_trial_armed(self, source_id: str) -> bool:
        """Return whether one process-local search trial is waiting for admission."""

        return source_id in self._search_reenable_trials

    def search_reenable_trial_pending(self, source_id: str) -> bool:
        """Return whether a one-shot search trial is armed or in flight."""

        return (
            source_id in self._search_reenable_trials
            or source_id in self._search_reenable_trials_inflight
        )

    def reserve_search_reenable_trial(self, source_id: str) -> bool:
        """Atomically consume the one-shot search trial for one candidate."""

        if source_id not in self._search_reenable_trials:
            return False
        self._search_reenable_trials.remove(source_id)
        self._search_reenable_trials_inflight.add(source_id)
        return True

    def settle_search_reenable_trial(self, source_id: str) -> None:
        """Forget a trial after its admitted search attempt settles."""

        self._search_reenable_trials_inflight.discard(source_id)

    def _drop_search_reenable_trial(self, source_id: str) -> None:
        self._search_trial_epochs[source_id] = self._search_trial_epochs.get(source_id, 0) + 1
        self._search_trial_identities.pop(source_id, None)
        self._search_reenable_trials.discard(source_id)
        self._search_reenable_trials_inflight.discard(source_id)

    def _mint_settlement_generation(self) -> int:
        self._next_settlement_generation += 1
        return self._next_settlement_generation

    def _reserve_settlement_generation(self, source_id: str, *, source: ModelHubSourceConfig | None = None) -> int:
        """Reserve on the loop; an already-admitted snapshot avoids a second config read."""
        generation = self._mint_settlement_generation()
        self._latest_source_attempt_generation[source_id] = generation
        if source is None:
            source = next((item for item in self.store.load().sources if item.id == source_id), None)
        if source is not None:
            self._source_attempt_identities[source_id] = source_identity(source)
        return generation

    def _settlement_current(self, source: ModelHubSourceConfig, generation: int | None) -> bool:
        return (
            generation is not None
            and generation >= self._latest_source_attempt_generation.get(source.id, generation)
            and self._source_attempt_identities.get(source.id, source_identity(source)) == source_identity(source)
        )

    def recovery_annotations(self, config: ModelHubConfig) -> dict[str, SourceRecoveryAnnotation]:
        """Read live health once; preview/guard drafts cannot mutate its owner."""

        current = self.store.load()
        annotations = self.recovery.annotations(current)
        identities = {source.id: source_identity(source) for source in current.sources}
        return {
            source.id: annotations[source.id]
            for source in config.sources
            if source.id in annotations and identities.get(source.id) == source_identity(source)
        }

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

    async def _repair_credential_addresses(self) -> None:
        """Take a credential's address out of ids an older release stored.

        Discovery no longer writes one, but a file written before it stopped
        can still hold an addressed id, and composition then addresses it a
        second time — the model the user picked resolves to nothing. Every
        demand for the engine passes here, so an installation upgrading into
        this release is repaired the first time it needs a model.

        This is the seam because it is the only one where both halves are in
        hand. The engine state store can prove which address belongs to which
        Source, so nothing is renamed on the strength of how it is spelled; and
        the whole config is loaded, so every collection keyed by a model id
        moves in one write instead of half the joins being left naming
        something gone. Persisting goes through the same projection owner as
        any other mutation, so the engine is reconciled with what was written.

        A stored id reaches past this config, too. A Vibe Agent's model, a
        channel's routing override, and a session's pin are each a copy of an
        id the menu once offered, and each is read back to decide what a turn
        asks for — repairing only the catalog would leave them naming a model
        that no longer exists under that name. The owner of those rows repairs
        them through ``repair_model_selections``, against this same proof.

        Best effort by design: an id left alone is exactly the state the
        previous release was already in, which is not worth failing a demand
        over. The next demand tries again.
        """

        async with self._mutation_lock:
            config = self.store.load()
            payload = config.to_payload()
            if not payload_carries_credential_address(payload):
                return
            addresses: dict[str, str] = {}
            for source in config.sources:
                if not source.credential_ref:
                    continue
                try:
                    address = await self.adapter.credential_address(
                        source.credential_ref
                    )
                except Exception:
                    # An address that cannot be read proves nothing about this
                    # Source's ids, and a guess is what this design avoids.
                    logger.debug(
                        "model hub: no provable address for source %s", source.id
                    )
                    continue
                if address:
                    addresses[source.id] = address
            try:
                repair = repair_credential_addresses(payload, addresses)
                if not repair.changed:
                    return
                # Selections move before the config is committed, because the
                # config is the witness that anything needs repairing at all.
                # Committing it first would clear that witness while a copy of
                # one of its ids is still stored as somebody's selection; this
                # order retries the whole pass instead, and both halves are
                # idempotent.
                selections = await self._repair_selections(
                    frozenset(addresses.values())
                )
                await self._commit_synced(
                    config,
                    ModelHubConfig.from_payload(repair.payload),
                )
            except Exception:
                logger.warning(
                    "model hub: could not repair stored credential addresses",
                    exc_info=True,
                )
                return
        logger.info(
            "model hub: removed credential addresses from stored ids "
            "(%d source models, %d route hops, %d routes, %d agent menu "
            "entries, %d model selections)",
            repair.models,
            repair.hops,
            repair.routes,
            repair.menu_entries,
            selections,
        )
        # A running backend answers from the catalog it was started with, so
        # reconciling the engine leaves it offering the ids that were just
        # renamed. Same follow-up any other catalog mutation makes.
        for backend in repair.backends:
            try:
                await self._refresh_backend_catalog(cast(BackendName, backend))
            except Exception:
                logger.warning(
                    "model hub: could not refresh %s after an address repair",
                    backend,
                    exc_info=True,
                )

    async def _repair_selections(self, addresses: frozenset[str]) -> int:
        """Hand the proven addresses to the owner of persisted selections."""

        if self.repair_model_selections is None or not addresses:
            return 0
        return await self.repair_model_selections(addresses)

    async def _sync_sources(self, config: ModelHubConfig, *, force_empty: bool = False) -> None:
        bindings = self._bindings(config)
        has_hub_sources = any(
            source.supply_channel == "hub" for source in config.sources
        )
        if not bindings and not force_empty and not has_hub_sources:
            self._engine_preparation_failed = False
            return
        # Reading the catalog copy takes tens of milliseconds; keep it off the loop.
        bindings = await asyncio.to_thread(_with_text_only_models, bindings)
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
        # Route target order is not part of this projection, so a pure chain reorder
        # compares equal and does not touch the engine.
        if previous_bindings == updated_bindings:
            await self._save_config(updated)
            return
        self._engine_synced = False
        await self._save_config(updated)
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
            await self._save_config(previous)
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
            await self._replay_credential_revocations_locked(
                self.store.load(), pending_revocations, sync_engine=True,
            )

    async def _replay_credential_revocations_locked(
        self,
        config: ModelHubConfig,
        pending_revocations: list[PendingCredentialRevocation],
        *,
        sync_engine: bool = False,
    ) -> list[PendingCredentialRevocation]:
        """Replay with shared custody guards and return remaining unowned intents."""

        takeover = self.migration_journal.load()
        takeover_credentials = {
            (credential["source_id"], credential["credential_ref"])
            for credential in (takeover or {}).get("credentials", [])
        }
        if sync_engine:
            await self._sync_sources(config, force_empty=bool(pending_revocations))
            self._engine_synced = True
        active_credentials = {
            source.id: source.credential_ref for source in config.sources
        }
        for pending in pending_revocations:
            if active_credentials.get(pending.source_id) == pending.credential_ref:
                try:
                    self.revocations.remove(pending.source_id, pending.credential_ref)
                except OSError:
                    pass
                continue
            if (pending.source_id, pending.credential_ref) in takeover_credentials:
                # A prepared takeover owns refs before they appear in config.
                # Other consumers cannot revoke its not-yet-committed grant.
                continue
            if pending.operation == "cleanup_orphaned_oauth_material":
                try:
                    cleaned = await self.adapter.cleanup_orphaned_oauth_material(pending.credential_ref)
                except Exception:
                    continue
                if not cleaned:
                    continue
            elif pending.operation in {"revoke_api_key_credential", "revoke_search_credential"}:
                revoker = (
                    self.adapter.revoke_search_credential
                    if pending.operation == "revoke_search_credential"
                    else self.adapter.revoke_api_key_credential
                )
                try:
                    await revoker(pending.credential_ref)
                except Exception:
                    # Only successful exact-namespace cleanup establishes
                    # absence. Metadata errors are not retirement proof.
                    continue
            else:
                try:
                    await self.adapter.revoke_credential(pending.credential_ref)
                except Exception as error:
                    if not self._credential_was_already_revoked(error):
                        continue
            try:
                self.revocations.remove(pending.source_id, pending.credential_ref)
            except OSError:
                pass

        # A retained intent is not necessarily failed cleanup: active Sources
        # and prepared takeovers still own their refs. Re-read after replay so
        # callers can gate new custody on the actual unowned postcondition.
        return [
            pending for pending in self.revocations.list()
            if active_credentials.get(pending.source_id) != pending.credential_ref
            and (pending.source_id, pending.credential_ref) not in takeover_credentials
        ]

    async def _require_api_key_cleanup_locked(self, config: ModelHubConfig) -> None:
        """Refuse new search custody while prior unowned API keys await cleanup."""

        try:
            pending = [
                entry for entry in self.revocations.list()
                if entry.operation in {"revoke_api_key_credential", "revoke_search_credential"}
            ]
            if not pending:
                return
            remaining = await self._replay_credential_revocations_locked(config, pending)
        except OSError:
            raise ModelHubError("engine_down", status=503) from None
        if any(entry.operation in {"revoke_api_key_credential", "revoke_search_credential"} for entry in remaining):
            # The journal owns the old ref, so a create nonce can be released,
            # but that durable ownership does not permit another credential.
            raise ModelHubError("engine_down", status=503)

    async def _prepare_engine_for_demand(self, *, already_synced: bool = False) -> None:
        try:
            await self._ensure_runtime_dependency()
            await self._repair_credential_addresses()
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
        """Recover credential custody, then resume a runtime the user left enabled.

        Only custody recovery gates service readiness. Resuming may first
        download the engine, so it runs in the background, retired by `stop()`;
        every consumer prepares the engine on demand in the meantime.
        """

        try:
            await recover_native_migration(self)
        except (TakeoverStateError, OSError):
            # Unknown recovery state cannot authorize any native credential
            # writer or native launch. Controller recovery preserves this gate.
            self.migration_blocked_backends.update(NATIVE_CLI_BACKENDS)
            raise ModelHubError("migration_item_conflict", status=409) from None
        task = self._runtime_resume_task
        if task is None or task.done():
            self._runtime_resume_task = asyncio.create_task(
                self._resume_runtime_intent(),
                name="model-hub-runtime-resume",
            )

    async def _resume_runtime_intent(self) -> None:
        try:
            await self.reconcile_runtime_installation()
            if not self.store.load().enabled:
                return
            # Preparing may download the engine. Like every demand path, it
            # holds no lifecycle lock, so an explicit start or stop is served
            # meanwhile instead of after the download.
            await self._prepare_engine_for_demand()
            async with self._runtime_lifecycle_lock:
                # An explicit stop clears the intent; `stop()` retires this
                # resume. Either way it must not start the engine.
                if self._runtime_resume_task is not asyncio.current_task():
                    return
                if not self.store.load().enabled:
                    return
                await self._engine_call(self.adapter.start())
        except Exception:
            logger.exception("Model Hub runtime resume failed; the engine is prepared on demand")

    async def _ensure_runtime_dependency(
        self,
        *,
        force: bool = False,
        offline: bool = False,
    ) -> EngineEnsureResult:
        return await ensure_runtime_dependency(
            self.adapter,
            force=force,
            offline=offline,
        )

    async def stop(self) -> None:
        task = self._migration_task
        if task is not None and not task.done():
            try:
                # A disconnected requester does not own the handoff lifetime.
                # Retire the runtime only once custody has settled or a durable
                # recovery record has captured the failure.
                await await_owned_task(task)
            except Exception:
                logger.warning("Native takeover remains pending during shutdown")
        resume, self._runtime_resume_task = self._runtime_resume_task, None
        if resume is not None and not resume.done():
            # Drain rather than cancel: the installer runs in a worker thread
            # that cancellation cannot stop, and the adapter must not stop
            # beneath it. A retired resume never starts the engine.
            await await_owned_task(resume)
        discovery, self._background_discovery_task = self._background_discovery_task, None
        viewed, self._viewed_discovery_task = self._viewed_discovery_task, None
        if self._background_discovery_stop is not None:
            self._background_discovery_stop.set()
        # Background listings never start the engine, and an upstream listing
        # is cancelled outright. An engine listing's worker is owned and
        # bounded, so these return only once nothing still uses the engine
        # that `adapter.stop()` retires.
        retiring = [task for task in (discovery, viewed, *self._search_reenable_checks.values())
                    if task is not None and not task.done()]
        for task in retiring:
            task.cancel()
        await asyncio.gather(*retiring, return_exceptions=True)
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

    async def _discover(
        self,
        source: ModelHubSourceConfig,
        *,
        start_engine: bool = True,
    ) -> list[DiscoveredModel]:
        if source.protocol == "search":
            return []
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
                    start_engine=start_engine,
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
                if pinned_protocol == "search":
                    raise ModelHubError("discovery_failed")
                return (pinned_protocol,)
            if vendor == "custom":
                # Google is explicit API/config admission in v11, not a new
                # automatic probe or picker option. Its generation path needs
                # a model, which non-inference observation must not invent.
                return tuple(protocol for protocol in MODEL_PROTOCOLS if protocol != "google")
            raise ModelHubError("discovery_failed")
        requested = payload.get("protocol")
        if not isinstance(requested, str) or requested not in MODEL_PROTOCOLS:
            raise ModelHubError("discovery_failed")
        if pinned_protocol is not None and requested != pinned_protocol:
            raise ModelHubError("discovery_failed")
        try:
            validate_model_hub_protocol_vendor(vendor, requested)
        except ValueError:
            raise ModelHubError("discovery_failed") from None
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
        if any(canonical_model_id(model.id) is None for model in validated.models):
            raise ModelHubError("discovery_failed", status=502)
        try:
            for model in validated.models:
                for parameter in model.supported_parameters or ():
                    parameter.encode("utf-8")
        except UnicodeEncodeError:
            raise ModelHubError("discovery_failed", status=502) from None
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
        auth_scheme: str | None = None,
        on_reserved: Callable[[str], None] | None = None,
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
            **({"auth_scheme": auth_scheme} if auth_scheme is not None else {}),
            on_reserved=on_reserved,
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
        *, auth_scheme: str | None = None, on_reserved: Callable[[str], None] | None = None,
    ) -> SourceObservation:
        return await self._observe_source_payload(
            payload, require_proven=True, on_reserved=on_reserved,
            **({"auth_scheme": auth_scheme} if auth_scheme is not None else {}),
        )

    async def _provision_oauth_credential(
        self,
        source_id: str,
        vendor: str,
        material: Mapping[str, object],
        *, on_reserved: Callable[[str], None] | None = None,
    ) -> str:
        return await self.adapter.provision_oauth_credential(
            source_id,
            vendor,
            material,
            on_reserved=on_reserved,
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
        *,
        operation: RevocationOperation | None = None,
    ) -> bool:
        source = next(
            (candidate for candidate in self.store.load().sources if candidate.id == source_id),
            None,
        )
        resolved_operation = operation or (
            "revoke_search_credential"
            if source is not None and source.protocol == "search"
            else "revoke_credential"
        )
        if resolved_operation == "revoke_search_credential":
            revoker = self.adapter.revoke_search_credential
        elif resolved_operation == "revoke_api_key_credential":
            revoker = self.adapter.revoke_api_key_credential
        else:
            revoker = self.adapter.revoke_credential
        journaled = False
        try:
            if resolved_operation == "revoke_credential":
                self.revocations.add(source_id, credential_ref)
            else:
                self.revocations.add(
                    source_id,
                    credential_ref,
                    operation=resolved_operation,
                )
        except OSError:
            pass
        else:
            journaled = True
        try:
            await revoker(credential_ref)
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
        # The material behind the ref may be the new grant's: the Source no longer has the earlier one's identity.
        self._mark_source_unverified(source)
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
        self.quota.forget(source_id)

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
            expired = parse_model_hub_timestamp(flow.expires_at_iso) <= self.now()
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
            catalog_efforts_by_model = bundled_catalog_wire_efforts_by_model()

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
        display_name_defaulted: bool = False,
    ) -> None:
        previous = previous or self.store.load()
        config = self._clone_config(previous)
        if any(item.id == source.id for item in config.sources):
            raise ModelHubError("migration_item_conflict", status=409)
        if source.kind == "subscription" and display_name_defaulted:
            names = {item.display_name for item in config.sources}
            seed = source.display_name
            number = 2
            while source.display_name in names:
                suffix = f" {number}"
                source.display_name = f"{seed[:64 - len(suffix)]}{suffix}"
                number += 1
        config.sources.append(source)
        if source.protocol == "search":
            _maintain_search_service_order(config, source.id, add=True)
        self._apply_source_placement(config, source)
        seeded = config.vibey_supply_pending and await self._seed_vibey(config)
        await self._commit_synced(previous, config)
        if seeded:
            await self._announce_vibey_seed()

    def _apply_source_placement(
        self,
        config: ModelHubConfig,
        source: ModelHubSourceConfig,
        backends: Iterable[str] = MODEL_HUB_BACKENDS,
    ) -> None:
        """Add a new Source to eligible backend defaults without editing overrides.

        A subscription serves a fixed catalog, so it joins only the backends whose
        menu that catalog serves (OpenCode and Vibey reach every
        vendor), ahead of the API keys there. API keys stay open to every
        eligible backend, appended.
        """

        for backend in backends:
            if backend == "vibey" and config.vibey_supply_pending:
                # Its seed places every Source at once, once the mutation's
                # Sources are final (see _seed_vibey).
                continue
            agent = config.agents[backend]
            if not self._eligible_for_agent(source, backend) or source.id in agent.sources.order:
                continue
            if source.kind != "subscription":
                agent.sources.order.append(source.id)
                continue
            if not self._subscription_serves_backend(agent, source, backend):
                continue
            by_id = {item.id: item for item in config.sources}
            position = next(
                (
                    index
                    for index, source_id in enumerate(agent.sources.order)
                    if (existing := by_id.get(source_id)) is not None and existing.kind != "subscription"
                ),
                len(agent.sources.order),
            )
            agent.sources.order.insert(position, source.id)

    @staticmethod
    def _subscription_serves_backend(
        agent: ModelHubAgentSupplyConfig,
        source: ModelHubSourceConfig,
        backend: BackendName,
    ) -> bool:
        if (
            backend not in _NATIVE_VENDOR_BACKENDS.values()
            or _NATIVE_VENDOR_BACKENDS.get(source.vendor) == backend
        ):
            # A backend that is no one vendor's client (OpenCode, Vibey)
            # reaches every vendor, and the vendor's own Agent serves its
            # subscription even when the catalog is ahead of the backend's menu.
            return True
        if not agent.models or not any(not model.retired for model in source.models):
            return False
        # Another backend serves it only where the catalogs overlap.
        return any(
            _matching_v1_model_id(
                backend=backend,
                requested_model=model.id,
                source=source,
                include_manual=True,
            ) is not None
            for model in agent.models
        )

    def _matching_menu_model_hops(
        self,
        config: ModelHubConfig,
        backend: BackendName,
        menu_model: str,
    ) -> tuple[ModelHubRouteHopConfig, ...]:
        """Expose matching inventory evidence for a prospective catalog row."""

        automatic = replace(
            config,
            agents={**config.agents, backend: replace(
                config.agents[backend],
                routes={},
                models=[ModelHubBackendModelConfig(id=menu_model, origin="manual")],
            )},
        )
        route = effective_model_route(automatic, backend, menu_model)
        return route.hops if route.route_origin == "automatic" else ()

    @staticmethod
    def _agent_menu_model_ids(agent: ModelHubAgentSupplyConfig) -> tuple[str, ...]:
        return tuple(model.id for model in agent.models)

    def _added_to(self, source_id: str) -> list[dict]:
        config = self.store.load()
        result: list[dict] = []
        for backend in MODEL_HUB_BACKENDS:
            agent = config.agents[backend]
            for menu_model in self._agent_model_ids(agent, self._requested_model(agent)):
                route = effective_model_route(config, cast(BackendName, backend), menu_model)
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
            for menu_model in self._agent_model_ids(config.agents[backend], self._requested_model(config.agents[backend]))
            if any(
                hop.source_id == source_id
                for hop in effective_model_route(config, cast(BackendName, backend), menu_model).hops
            )
        ]
        result.sort(key=lambda item: (item["backend"], item["menu_model"]))
        return result

    def _source_payload(
        self,
        source: ModelHubSourceConfig,
        config: ModelHubConfig | None = None,
    ) -> dict:
        payload = source.to_payload()
        observed_at = self.now()
        for model in payload["models"]:
            model.setdefault("retired", False)
            refused = self._refused_efforts(source, model["id"], observed_at)
            if refused:
                model["refused_efforts"] = refused
        payload["adopted_by"] = self._adopted_by(source.id, config)
        if source.protocol == "search":
            payload["trial_pending"] = self.search_reenable_trial_pending(source.id)
        return self._source_account_payload(payload)

    def _source_account_payload(self, payload: dict) -> dict:
        # Resolve from the current binding on every presentation, including
        # pre-existing subscriptions and grants changed by re-authentication.
        # Adapters without this optional metadata surface retain persisted labels.
        reader = getattr(self.adapter, "subscription_account_label", None)
        if (
            callable(reader)
            and payload["kind"] == "subscription"
            and payload["supply_channel"] == "hub"
            and payload.get("credential_ref")
        ):
            payload["account_label"] = reader(payload["id"], payload["vendor"], payload["credential_ref"])
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
            "source": self._source_account_payload(source),
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
        display_name_defaulted: bool = False,
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
                    observation = await self._observe_provisioned_credential(
                        vendor,
                        None,
                        rollback_credential_ref,
                        MODEL_PROTOCOLS,
                    )
                    # Completed consent supplies credential custody, not proof
                    # that a model call works. Its vendor fixes the interface.
                    protocol = observation.protocol or _FIXED_BACKEND_PROTOCOLS.get(
                        _NATIVE_VENDOR_BACKENDS.get(vendor, vendor),
                    )
                    if protocol is None:
                        raise ModelHubError("discovery_failed")
                    if observation.discovery is ObservationDiscovery.SUCCEEDED:
                        discovered = list(observation.models)
                    elif observation.discovery is ObservationDiscovery.FAILED:
                        discovered = None
                        state = ModelHubSourceStateConfig(
                            status="error",
                            detail_key="models.source.error.unclassified",
                        )
                    else:
                        discovered = None
                        if observation.authenticated is False:
                            state = ModelHubSourceStateConfig(
                                status="needs_action",
                                detail_key="models.source.needs_action.oauth_expired",
                            )
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
                if channel == "hub":
                    self._mark_source_unverified(source)
                if (
                    channel == "hub"
                    and observation.outcome is not ObservationOutcome.OBSERVED
                    and observation.authenticated is not False
                ):
                    try:
                        discovered = await self._discover(source)
                    except ModelHubError as exc:
                        if exc.code != "discovery_failed":
                            raise
                        # Inventory is optional and never verifies the login.
                        discovered = None
                if discovered is not None:
                    self._apply_discovered_models(
                        source,
                        manual_models,
                        discovered,
                        allow_empty=channel == "hub",
                    )
                else:
                    catalog_efforts_by_model = (
                        bundled_catalog_wire_efforts_by_model()
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
                    display_name_defaulted=display_name_defaulted,
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
        application_failed = False
        if binding.channel == "native_cli" and binding.vendor == "anthropic" and not failed:
            try:
                # The existing flow retains this historical receipt for its
                # normal TTL. No status CLI or new persistence is involved.
                receipt = self.native_oauth_adapter.completed_source_status(flow_id)
                application_failed = getattr(receipt, "application_failed", False) is True
            except (KeyError, NativeOAuthUnavailableError):
                pass  # An expired receipt does not undo a saved Source.
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
            application_failed=application_failed,
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
                self._mark_source_unverified(source)
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
                self.quota.forget(source.id)
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
        self.quota.forget(source.id)
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
            display_name=seeded_source_name(binding.vendor),
            display_name_defaulted=True,
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

    async def _search_free_check(
        self,
        source: ModelHubSourceConfig,
    ) -> Literal["pass", "balance", "limit", "invalid", "unknown"]:
        service = _search_service_for_source(source)
        checker = getattr(self.adapter, "check_search_credential", None)
        if service is None or not callable(checker) or not source.credential_ref:
            return "unknown"
        catalog = {"vendor": source.vendor, **service}
        try:
            result = await checker(source.credential_ref, catalog)
        except asyncio.CancelledError:
            raise
        except Exception:
            return "unknown"
        return _search_check_outcome(result)

    async def _create_search_source(
        self,
        *,
        vendor: str,
        display_name: str,
        display_name_defaulted: bool,
        client_nonce: str | None,
        key: str,
    ) -> dict:
        service = search_service(vendor, "search")
        if service is None or not service.get("tool_only"):
            raise ModelHubError("discovery_failed")
        source = ModelHubSourceConfig(
            id=_source_id(),
            kind="api_key",
            vendor=vendor,
            display_name=display_name,
            protocol="search",
            supply_channel="hub",
            billing="metered",
            state=ModelHubSourceStateConfig(status="standby"),
            models=[],
            created_at=self.now().isoformat(),
            client_nonce=client_nonce,
            credential_ref="cred_preflight",
        )
        source = ModelHubSourceConfig.from_payload(source.to_payload())
        nonce_claimed = False
        release_nonce = True
        try:
            async with self._mutation_lock:
                if client_nonce is not None:
                    self._claim_source_create_nonce_locked(client_nonce)
                    nonce_claimed = True
                # Typed cleanup is engine-free; OAuth/generic intents remain
                # with model demand. Replaying alone is not admission proof.
                await self._require_api_key_cleanup_locked(self.store.load())
            credential_ref = await _acquire_credential_ref_with_cancellation_ownership(
                self,
                self.adapter.provision_credential(vendor, "search", key, None),
                source.id,
                rollback_operation="revoke_search_credential",
            )
            persisted = False
            try:
                source.credential_ref = credential_ref
                source.masked_credential = _mask_credential(key)
                outcome = await self._search_free_check(source)
                if outcome == "invalid":
                    raise ModelHubError(
                        "discovery_failed",
                        status=422,
                        detail="modelHub.errors.authentication_failed",
                    )
                if outcome in {"balance", "limit"}:
                    source.state = ModelHubSourceStateConfig(
                        status="needs_action",
                        detail_key=(
                            "models.source.needs_action.balance_exhausted"
                            if outcome == "balance"
                            else "models.source.needs_action.spend_limit_reached"
                        ),
                    )
                elif outcome == "unknown":
                    self._mark_source_unverified(source)
                else:
                    source.verification_pending = None
                source = ModelHubSourceConfig.from_payload(source.to_payload())
                async with self._mutation_lock:
                    await self._commit_new_source_locked(
                        source,
                        display_name_defaulted=display_name_defaulted,
                    )
                    persisted = True
                return self._source_creation_result(source.to_payload())
            except asyncio.CancelledError:
                persisted = self._persisted_credential(self.store.load(), source.id, credential_ref)
                if not persisted:
                    await _rollback_credential_before_settling(
                        self, source.id, credential_ref, operation="revoke_search_credential",
                    )
                raise
            except Exception:
                if not persisted:
                    await _rollback_credential_before_settling(
                        self, source.id, credential_ref, operation="revoke_search_credential",
                    )
                raise
        except CredentialCleanupUnsettledError:
            release_nonce = False
            raise
        finally:
            if nonce_claimed and release_nonce:
                self._source_create_nonces.discard(cast(str, client_nonce))

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
            "save_unverified",
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
        # A blank name is no name, so the Source starts with the vendor's seed.
        requested_name = payload.get("display_name")
        if isinstance(requested_name, str):
            requested_name = requested_name.strip()
        display_name_defaulted = not requested_name
        if kind not in {"subscription", "api_key"}:
            raise ModelHubError("discovery_failed")
        display_name = _validated_source_name(requested_name or seeded_source_name(vendor))
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
                # may be created with its models inline, so these newly supplied
                # identities must be representable before any credential work.
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
        save_unverified = payload.get("save_unverified", False)
        if (
            not isinstance(accept_unavailable_inventory, bool)
            or not isinstance(save_unverified, bool)
            or (
                kind != "api_key"
                and (
                    "protocol" in payload
                    or "accept_unavailable_inventory" in payload
                    or "save_unverified" in payload
                )
            )
        ):
            raise ModelHubError("discovery_failed")
        requested_protocol = (
            payload.get("protocol")
            if "protocol" in payload
            else pinned_api_key_protocol(vendor)
        )
        if kind == "api_key" and requested_protocol == "search":
            if (
                base_url is not None
                or billing != "metered"
                or manual_models
                or bool(payload.get("save_unverified"))
                or bool(payload.get("accept_unavailable_inventory"))
                or search_service(vendor, "search") is None
            ):
                raise ModelHubError("discovery_failed")
            return await self._create_search_source(
                vendor=vendor,
                display_name=display_name,
                display_name_defaulted=display_name_defaulted,
                client_nonce=client_nonce,
                key=cast(str, credential_value),
            )

        protocol_order: tuple[str, ...] | None = None
        if kind == "api_key":
            protocol_order = self._observation_protocols(vendor, payload)
            if save_unverified and len(protocol_order) != 1:
                raise ModelHubError("discovery_failed", detail="modelHub.errors.ambiguous_source")

        if oauth_ref:
            return self._source_creation_result(
                await self._create_oauth_source(
                    manual_models,
                    display_name=display_name,
                    display_name_defaulted=display_name_defaulted,
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
                        Literal["anthropic", "openai_responses", "openai_chat", "google"],
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
            observation = (
                None if save_unverified
                else await self._require_proven_source_payload(observation_payload)
            )
            if (
                observation is not None
                and observation.discovery is ObservationDiscovery.FAILED
                and not accept_unavailable_inventory
            ):
                raise ModelHubError(
                    "discovery_failed",
                    status=422,
                    detail="modelHub.errors.inventory_unavailable",
                    data={"observation": self._observation_payload(observation)},
                )
            if observation is None:
                # The marker records that nothing upstream has accepted this
                # credential yet, so the first successful call can retire it.
                # An observation reaching here is authenticated -- the terminal
                # product admits no other `observed` -- and that acceptance is
                # what the marker was waiting for; keeping it on a Source the
                # user just watched verify would make it mean something else.
                # Only the explicit unverified save arrives with none.
                self._mark_source_unverified(source)
            if observation is not None:
                source.protocol = cast(
                    Literal["anthropic", "openai_responses", "openai_chat", "google"],
                    observation.protocol,
                )
            if observation is not None and observation.discovery is ObservationDiscovery.SUCCEEDED:
                self._apply_discovered_models(
                    source,
                    manual_models,
                    list(observation.models),
                    allow_empty=True,
                )
            else:
                catalog_efforts_by_model = bundled_catalog_wire_efforts_by_model()
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
                if observation is not None:
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
            persisted = False
            try:
                source.credential_ref = rollback_credential_ref
                source.masked_credential = _mask_credential(cast(str, credential_value))
                if _search_service_for_source(source) is not None:
                    outcome = await self._search_free_check(source)
                    if outcome == "invalid":
                        raise ModelHubError(
                            "discovery_failed",
                            status=422,
                            detail="modelHub.errors.authentication_failed",
                        )
                    if outcome in {"balance", "limit"}:
                        source.verification_pending = None
                        source.state = ModelHubSourceStateConfig(
                            status="needs_action",
                            detail_key=(
                                "models.source.needs_action.balance_exhausted"
                                if outcome == "balance"
                                else "models.source.needs_action.spend_limit_reached"
                            ),
                        )
                    elif outcome == "pass":
                        source.verification_pending = None
                    elif outcome == "unknown" and source.protocol == "search":
                        self._mark_source_unverified(source)
                source = ModelHubSourceConfig.from_payload(source.to_payload())
                if save_unverified:
                    # Inventory is useful, not admission evidence. Work on a
                    # clone so a failed listing cannot partly replace manual
                    # entries. The new Source still has one commit/nonce owner.
                    candidate = ModelHubSourceConfig.from_payload(source.to_payload())
                    try:
                        discovered = await asyncio.wait_for(
                            self._discover(candidate), timeout=_SOURCE_DISCOVERY_TIMEOUT_SECONDS,
                        )
                        self._apply_discovered_models(
                            candidate, candidate.models, discovered, allow_empty=True,
                        )
                    except (ModelHubError, asyncio.TimeoutError):
                        pass
                    else:
                        source = candidate
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
                    await _rollback_credential_before_settling(
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

    async def _replace_search_credential_locked(
        self,
        previous: ModelHubConfig,
        config: ModelHubConfig,
        source: ModelHubSourceConfig,
        key: str,
    ) -> dict:
        service = _require_search_service_for_source(source)
        old_credential_ref = source.credential_ref
        if not old_credential_ref:
            raise ModelHubError("discovery_failed")
        await self._require_api_key_cleanup_locked(previous)
        committed = False
        old_revocation_recorded = False
        replacement_ref = await _acquire_credential_ref_with_cancellation_ownership(
            self,
            self.adapter.provision_credential(
                source.vendor,
                "search",
                key,
                None,
            ),
            source.id,
            rollback_operation="revoke_search_credential",
        )
        try:
            source.credential_ref = replacement_ref
            source.masked_credential = _mask_credential(key)
            self._mark_source_unverified(source)
            has_free_check = isinstance(service.get("free_check"), Mapping)
            outcome = await self._search_free_check(source)
            if has_free_check and outcome == "unknown":
                raise ModelHubError(
                    "discovery_failed",
                    status=502,
                    detail="modelHub.errors.credential_replacement_unverified",
                )
            if outcome == "invalid":
                raise ModelHubError(
                    "discovery_failed",
                    status=422,
                    detail="modelHub.errors.authentication_failed",
                )
            if outcome != "unknown":
                source.verification_pending = None
            source.state = ModelHubSourceStateConfig(status="standby")
            if outcome in {"balance", "limit"}:
                source.state = ModelHubSourceStateConfig(
                    status="needs_action",
                    detail_key=(
                        "models.source.needs_action.balance_exhausted"
                        if outcome == "balance"
                        else "models.source.needs_action.spend_limit_reached"
                    ),
                )
            if old_credential_ref != replacement_ref:
                self.revocations.add(
                    source.id,
                    old_credential_ref,
                    operation="revoke_search_credential",
                )
                old_revocation_recorded = True

            await self._commit_synced(previous, config)
            committed = True
        except asyncio.CancelledError:
            committed = self._persisted_credential(self.store.load(), source.id, replacement_ref)
            if not committed:
                await _rollback_replacement_before_settling(
                    self, source.id, replacement_ref, old_credential_ref,
                    old_revocation_recorded=old_revocation_recorded,
                )
            raise
        except Exception:
            if not committed:
                await _rollback_replacement_before_settling(
                    self, source.id, replacement_ref, old_credential_ref,
                    old_revocation_recorded=old_revocation_recorded,
                )
            raise
        finally:
            # Persistence, not request success, retires the old trial. This
            # also covers cancellation after the shielded config write landed.
            if self._persisted_credential(self.store.load(), source.id, replacement_ref):
                self._drop_search_reenable_trial(source.id)
        if old_credential_ref != replacement_ref:
            try:
                await self.adapter.revoke_search_credential(old_credential_ref)
            except Exception:
                pass
            else:
                try:
                    self.revocations.remove(source.id, old_credential_ref)
                except OSError:
                    pass
        return {"source": self._source_payload(source), "removed_hops": [], "interrupted": []}

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
            if source.protocol == "search":
                return await self._replace_search_credential_locked(
                    previous,
                    config,
                    source,
                    key,
                )
            if _search_service_for_source(source) is not None:
                self._drop_search_reenable_trial(source.id)

            old_credential_ref = source.credential_ref
            auth_scheme = await self._engine_call(
                self.adapter.credential_auth_scheme(old_credential_ref)
            )
            replacement_ref = await self._engine_call(
                self.adapter.provision_credential(
                    source.vendor,
                    source.protocol,
                    key,
                    source.base_url,
                    **({"auth_scheme": auth_scheme} if auth_scheme is not None else {}),
                )
            )
            committed = False
            old_revocation_recorded = False
            try:
                source.credential_ref = replacement_ref
                self._mark_source_unverified(source)
                source.masked_credential = _mask_credential(key)
                search_balance = False
                if _search_service_for_source(source) is not None:
                    outcome = await self._search_free_check(source)
                    if outcome == "invalid":
                        raise ModelHubError(
                            "discovery_failed",
                            status=422,
                            detail="modelHub.errors.authentication_failed",
                        )
                    source.verification_pending = None
                    if outcome in {"balance", "limit"}:
                        search_balance = True
                        source.state = ModelHubSourceStateConfig(
                            status="needs_action",
                            detail_key=(
                                "models.source.needs_action.balance_exhausted"
                                if outcome == "balance"
                                else "models.source.needs_action.spend_limit_reached"
                            ),
                        )
                    elif outcome == "unknown":
                        self._mark_source_unverified(source)
                discovered = (
                    [DiscoveredModel(id=model.id) for model in source.models if not model.retired]
                    if search_balance
                    else await self._discover(source)
                )
                if old_credential_ref != replacement_ref:
                    if any(
                        pending.source_id == source.id
                        and pending.credential_ref == old_credential_ref
                        and pending.operation == "revoke_credential"
                        for pending in self.revocations.list()
                    ):
                        # Replay also discards intents for the current active
                        # ref. Do that here for a pre-upgrade generic intent:
                        # syncing the damaged old credential is not required
                        # to establish that it is still the committed ref.
                        self.revocations.remove(source.id, old_credential_ref)
                    self.revocations.add(
                        source.id, old_credential_ref, operation="revoke_api_key_credential",
                    )
                    old_revocation_recorded = True
                if search_balance:
                    removed_hops, interrupted = self._guard_inventory_mutation(
                        previous,
                        config,
                        source.id,
                        force=force,
                        confirmed_remove_hops=payload.get("would_remove_hops"),
                        confirmed_interruptions=payload.get("would_interrupt"),
                    )
                    await self._commit_synced(previous, config)
                else:
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
                    await self.adapter.revoke_api_key_credential(old_credential_ref)
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
            if source.protocol == "search":
                _require_search_service_for_source(source)
            removed_hops: list[dict] = []
            interrupted: list[dict] = []
            if "display_name" in payload:
                source.display_name = _validated_source_name(payload["display_name"])
            base_url_changed = (
                "base_url" in payload and source.base_url != base_url
            )
            if base_url_changed:
                if (
                    source.kind != "api_key"
                    or source.protocol == "search"
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
                    self._mark_source_unverified(source)
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

    async def _reenable_search_service(self, source_id: str, payload: object) -> dict:
        """One shared free check, or one fail-closed trial, fenced against later attempts and identity changes."""
        if payload not in (None, {}):
            raise ModelHubError("invalid_parameter", status=400)
        async with self._mutation_lock:
            joined = self._search_reenable_checks.get(source_id)
            if joined is None:
                source = self._source(await asyncio.to_thread(self.store.load), source_id)
                catalog = _require_search_service_for_source(source)
                if source.protocol != "search" or source.state.status != "needs_action" or source.state.detail_key not in {
                    "models.source.needs_action.balance_exhausted", "models.source.needs_action.spend_limit_reached",
                }:
                    raise ModelHubError("discovery_failed")
                if self.search_reenable_trial_pending(source_id):
                    return {"source": await asyncio.to_thread(self._source_payload, source), "removed_hops": [], "interrupted": [], "trial_armed": True}
                generation = self._reserve_settlement_generation(source_id, source=source)
                self._service_cooldowns.advance(source, catalog["service_id"], clear=True)
                check = catalog.get("free_check")
                needed = "balance" if source.state.detail_key.endswith("balance_exhausted") else "limit"
                if not isinstance(check, Mapping) or not check.get(needed):
                    self._search_reenable_trials.add(source_id)
                    self._search_trial_identities[source_id] = source_identity(source)
                    self._search_trial_epochs[source_id] = self._search_trial_epochs.get(source_id, 0) + 1
                    return {"source": await asyncio.to_thread(self._source_payload, source), "removed_hops": [], "interrupted": [], "trial_armed": True}

                async def checked() -> dict:
                    try:
                        outcome = await self._search_free_check(source)
                        async with self._mutation_lock:
                            previous = await asyncio.to_thread(self.store.load)
                            current = next((s for s in previous.sources if s.id == source_id), None)
                            if current is None:
                                raise ModelHubError("source_not_found", status=404)
                            if source_identity(current) == source_identity(source) and self._settlement_current(current, generation):
                                config = await asyncio.to_thread(self._clone_config, previous)
                                updated = self._source(config, source_id)
                                if outcome in {"pass", "balance", "limit"}:
                                    updated.verification_pending = None
                                detail = {
                                    "invalid": "credential_revoked", "balance": "balance_exhausted", "limit": "spend_limit_reached",
                                }.get(outcome)
                                if outcome == "pass":
                                    updated.state = ModelHubSourceStateConfig(status="standby")
                                elif detail:
                                    updated.state = ModelHubSourceStateConfig(status="needs_action", detail_key=f"models.source.needs_action.{detail}")
                                if updated != current:
                                    await self._save_runtime_config(previous, config)
                                    current = self._source(await asyncio.to_thread(self.store.load), source_id)
                            return {"source": await asyncio.to_thread(self._source_payload, current), "removed_hops": [], "interrupted": [], "trial_armed": False}
                    finally:
                        if self._search_reenable_checks.get(source_id) is asyncio.current_task():
                            self._search_reenable_checks.pop(source_id, None)

                joined = asyncio.create_task(checked())
                self._search_reenable_checks[source_id] = joined
        return await asyncio.shield(joined)

    async def reenable_search_source(self, source_id: str, payload: object = None) -> dict:
        if hosted_tools_module.SEARCH_SERVICES_ACTIVE:
            return await self._reenable_search_service(source_id, payload)
        if payload not in (None, {}):
            raise ModelHubError("discovery_failed")
        async with self._mutation_lock:
            previous = self.store.load()
            config = self._clone_config(previous)
            source = self._source(config, source_id)
            if source.protocol != "search" or source.kind != "api_key":
                raise ModelHubError("discovery_failed")
            service = _require_search_service_for_source(source)
            if source.state.status != "needs_action" or source.state.detail_key not in {
                "models.source.needs_action.balance_exhausted",
                "models.source.needs_action.spend_limit_reached",
            }:
                raise ModelHubError("discovery_failed")
            if self.search_reenable_trial_pending(source.id):
                return {
                    "source": self._source_payload(source, config),
                    "removed_hops": [],
                    "interrupted": [],
                    "trial_armed": True,
                }
            trial_armed = False
            has_free_check = isinstance(service.get("free_check"), Mapping)
            if not has_free_check:
                self._search_reenable_trials.add(source.id)
                trial_armed = True
            else:
                outcome = await self._search_free_check(source)
                if outcome in {"pass", "balance", "limit"}:
                    source.verification_pending = None
                if outcome == "pass":
                    source.state = ModelHubSourceStateConfig(status="standby")
                    self._drop_search_reenable_trial(source.id)
                elif outcome == "invalid":
                    source.state = ModelHubSourceStateConfig(
                        status="needs_action",
                        detail_key="models.source.needs_action.credential_revoked",
                    )
                    self._drop_search_reenable_trial(source.id)
                elif outcome == "balance":
                    source.state = ModelHubSourceStateConfig(
                        status="needs_action",
                        detail_key="models.source.needs_action.balance_exhausted",
                    )
                    self._drop_search_reenable_trial(source.id)
                elif outcome == "limit":
                    source.state = ModelHubSourceStateConfig(
                        status="needs_action",
                        detail_key="models.source.needs_action.spend_limit_reached",
                    )
                    self._drop_search_reenable_trial(source.id)
                else:
                    self._drop_search_reenable_trial(source.id)
            if source.state.status != "needs_action" or not trial_armed:
                await self._commit_synced(previous, config)
            return {
                "source": self._source_payload(source),
                "removed_hops": [],
                "interrupted": [],
                "trial_armed": trial_armed,
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
                add(
                    self._menu_model_for_selection(agent, str(pinned_model or "").strip()),
                    name,
                )
        for model in agent.models:
            add(model.id)
        for model_id, route in agent.routes.items():
            if normalized_model_hub_override(route) is not None:
                add(model_id)
        return protected

    def _removed_agent_models(
        self,
        previous: ModelHubConfig,
        updated: ModelHubConfig,
        backend: BackendName,
        removed_model_ids: set[str],
    ) -> list[dict]:
        """The Agents a model-list removal stops, as supply gaps.

        An Agent is affected only when the row its selection names before the
        save is removed. If its selection then names no surviving row, the
        removal stops it whatever supply the row had, and the gap carries the
        removed row's id. An OpenCode selection can still name a surviving row
        (``openai/foo`` falls back to ``foo``); it is stopped only when that row
        has no runnable hop, and the gap carries the row it now runs on. Both
        lists are matched the way the turn path matches them.
        """

        before = previous.agents[backend]
        after = updated.agents[backend]
        if before.mode != "hub" or self.named_agents_override is None:
            return []
        unavailable_source_ids = self._unavailable_native_sources(updated, backend)
        live_recovery = self.recovery_annotations(updated)
        surviving_ids = {model.id for model in after.models}
        agents_by_model: dict[str, set[str]] = {}
        for name, selected in self.named_agents_override(backend):
            selected = str(selected or "").strip()
            if self._menu_model_for_selection(before, selected) not in removed_model_ids:
                continue
            model_id = self._menu_model_for_selection(after, selected)
            if model_id in surviving_ids and resolve_model_hub_turn(
                updated,
                backend,
                model_id,
                now=self.now(),
                unavailable_source_ids=unavailable_source_ids,
                live_recovery=live_recovery,
            ).candidates:
                continue
            if model_id not in surviving_ids:
                model_id = self._menu_model_for_selection(before, selected)
            agents_by_model.setdefault(model_id, set()).add(name)
        return [
            {"backend": backend, "model_id": model_id, "agents": sorted(names)}
            for model_id, names in sorted(agents_by_model.items())
        ]

    def _would_interrupt(
        self,
        config: ModelHubConfig,
        *,
        include_unconfigured: bool = False,
    ) -> list[dict]:
        gaps: list[dict] = []
        for backend_name in MODEL_HUB_BACKENDS:
            backend = cast(BackendName, backend_name)
            unavailable_source_ids = self._unavailable_native_sources(config, backend)
            protected = self._protected_menu_models(config, backend)
            for model_id, agents in protected.items():
                resolution = resolve_model_hub_turn(
                    config,
                    backend,
                    model_id,
                    now=self.now(),
                    unavailable_source_ids=unavailable_source_ids,
                    live_recovery=self.recovery_annotations(config),
                )
                if resolution.candidates:
                    continue
                if not include_unconfigured and not agents and not resolution.inspected_hops:
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
    ) -> list[dict]:
        baseline = {
            (item["backend"], item["model_id"])
            for item in self._would_interrupt(previous, include_unconfigured=True)
        }
        return [
            item
            for item in self._would_interrupt(
                updated,
                include_unconfigured=True,
            )
            if (item["backend"], item["model_id"]) not in baseline
        ]

    def _effective_hop_refs(self, config: ModelHubConfig, backend: str | None = None) -> list[dict]:
        refs: list[dict] = []
        for backend_name in MODEL_HUB_BACKENDS:
            if backend is not None and backend_name != backend:
                continue
            agent = config.agents[backend_name]
            model_ids = dict.fromkeys((
                *self._agent_model_ids(agent, self._requested_model(agent)),
                *self._protected_menu_models(config, cast(BackendName, backend_name)),
            ))
            for model_id in model_ids:
                route = effective_model_route(config, cast(BackendName, backend_name), model_id)
                refs.extend(
                    {
                        "backend": backend_name,
                        "menu_model": model_id,
                        "source_id": hop.source_id,
                        "model_id": hop.model_id,
                        "position": position,
                    }
                    for position, hop in enumerate(route.hops, start=1)
                )
        return refs

    def _removed_effective_hops(
        self,
        previous: ModelHubConfig,
        updated: ModelHubConfig,
        *,
        backend: str | None = None,
        model_id: str | None = None,
    ) -> list[dict]:
        def identity(item: dict) -> tuple[str, str, str, str]:
            return item["backend"], item["menu_model"], item["source_id"], item["model_id"]

        remaining = {identity(item) for item in self._effective_hop_refs(updated, backend)}
        return [
            item for item in self._effective_hop_refs(previous, backend)
            if (model_id is None or item["menu_model"] == model_id) and identity(item) not in remaining
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
            agent = config.agents[backend]
            for menu_model, route in list(agent.routes.items()):
                route.hops = tuple(
                    hop
                    for hop in route.hops
                    if (backend, menu_model, hop.source_id, hop.model_id)
                    not in identities
                )
                if normalized_model_hub_override(route) is None:
                    agent.routes.pop(menu_model)

    def _inventory_removed_hops(
        self,
        previous: ModelHubConfig,
        updated: ModelHubConfig,
    ) -> list[dict]:
        # Inventory evidence narrows speculative candidates without deleting routes.
        return [
            item for item in self._removed_effective_hops(previous, updated)
            if not (
                effective_model_route(
                    previous, cast(BackendName, item["backend"]), item["menu_model"],
                ).route_origin == "passthrough"
                and effective_model_route(
                    updated, cast(BackendName, item["backend"]), item["menu_model"],
                ).route_origin == "automatic"
            )
        ]

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
        invalidated = self._invalidated_route_hops(updated, source_id)
        self._prune_invalidated_route_hops(updated, invalidated)
        # Keep explicit invalidations and newly introduced supply gaps guarded below.
        would_remove_hops = self._inventory_removed_hops(previous, updated)
        would_remove_hops.extend(item for item in invalidated if item not in would_remove_hops)
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
            removed_hops = [item for item in self._effective_hop_refs(previous) if item["source_id"] == source_id]
            config.sources = [item for item in config.sources if item.id != source_id]
            if source.protocol == "search":
                _maintain_search_service_order(config, source_id, add=False)
            for agent in config.agents.values():
                agent.sources.order = [item for item in agent.sources.order if item != source_id]
                for model_id, route in list(agent.routes.items()):
                    route.hops = tuple(hop for hop in route.hops if hop.source_id != source_id)
                    if normalized_model_hub_override(route) is None:
                        agent.routes.pop(model_id)
            would_interrupt = self._introduced_interruptions(
                previous,
                config,
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
            if source.credential_ref:
                self.revocations.add(
                    source.id,
                    source.credential_ref,
                    operation=("revoke_search_credential" if source.protocol == "search" else "revoke_credential"),
                )
            try:
                await self._commit_synced(previous, config)
            except Exception:
                if source.credential_ref:
                    self.revocations.remove(source.id, source.credential_ref)
                raise
            finally:
                if not any(item.id == source_id for item in self.store.load().sources):
                    self._drop_search_reenable_trial(source_id)
            try:
                if source.credential_ref:
                    revoke = (
                        self.adapter.revoke_search_credential(source.credential_ref)
                        if source.protocol == "search"
                        else self.adapter.revoke_credential(source.credential_ref)
                    )
                    await self._engine_call(revoke)
            except ModelHubError:
                if source.protocol == "search":
                    # The config deletion is already durable. A search Source
                    # has no engine binding to restore, and the journal must
                    # survive any partial credential retirement for replay.
                    raise
                restored = False
                try:
                    await self._save_config(previous)
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
            if source.supply_channel == "native_cli" or source.protocol == "search":
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

    def start_background_discovery(self) -> None:
        """Keep Hub Source inventories current until `stop()` retires the schedule."""

        task = self._background_discovery_task
        if task is not None and not task.done():
            return
        stopping = asyncio.Event()
        self._background_discovery_stop = stopping
        self._background_discovery_task = asyncio.create_task(
            self._background_discovery_loop(stopping),
            name="model-hub-background-discovery",
        )

    async def _background_discovery_loop(self, stopping: asyncio.Event) -> None:
        while True:
            try:
                await asyncio.wait_for(
                    stopping.wait(), _BACKGROUND_DISCOVERY_TICK.total_seconds(),
                )
                return
            except asyncio.TimeoutError:
                pass
            await self._refresh_due_sources_logged()

    async def _refresh_due_sources_logged(self, *, max_age: timedelta | None = None) -> None:
        try:
            await self._refresh_due_sources(max_age=max_age)
        except Exception as exc:
            logger.warning(
                "Model Hub background discovery pass failed: %s", type(exc).__name__,
            )

    def request_inventory_refresh(self) -> None:
        """A viewer read Source inventory: list Sources older than 30 minutes.

        Fire-and-forget and coalesced into one pass. It runs only while the
        background schedule does, so it shares that schedule's backoff and
        shutdown and never starts work for a stopped runtime.
        """

        self._request_discovery_pass(max_age=_VIEWED_DISCOVERY_AGE)

    def mark_stale_listing(self, source: ModelHubSourceConfig, model_id: str) -> None:
        """A Source refused a model it lists: have the schedule list it again soon (#2682).

        Only a mark: the schedule's own due rule lists the Source once its
        listing is older than ``_STALE_LISTING_AGE``, under its eligibility,
        backoff, in-flight exclusion and shutdown, and that listing drops each
        marked row if the identity that refused it still holds and the listing
        no longer names it either. A refusal is filed under the identity that
        gave it, so a turn still running on a replaced key or URL never
        displaces the current identity's refusals (#2777).
        """

        marks = self._stale_listings.setdefault(source.id, {})
        identity = source_identity(source)
        # Turns frozen on different executor models of one Source may each be refused.
        marks[identity] = marks.get(identity, frozenset()) | {model_id}
        self._request_discovery_pass(max_age=None)

    def _request_discovery_pass(self, *, max_age: timedelta | None) -> None:
        schedule = self._background_discovery_task
        stopping = self._background_discovery_stop
        if schedule is None or schedule.done() or stopping is None or stopping.is_set():
            return
        viewed = self._viewed_discovery_task
        if viewed is not None and not viewed.done():
            return
        self._viewed_discovery_task = asyncio.get_running_loop().create_task(
            self._refresh_due_sources_logged(max_age=max_age),
            name="model-hub-viewed-discovery",
        )

    async def _refresh_due_sources(self, *, max_age: timedelta | None = None) -> None:
        """Refresh, one at a time, each Hub Source whose background listing is due.

        ``max_age`` tightens the period for a viewer's pass; backoff still holds.
        """

        config = self.store.load()
        if not config.enabled:
            # A stopped runtime is an explicit opt-out, and an OAuth listing
            # would start the engine.
            return
        sources = [
            source for source in config.sources
            if self._background_discovery_eligible(source)
        ]
        live_ids = {source.id for source in sources}
        for table in (self._background_discovery_checked, self._background_discovery_retries, self._stale_listings):
            for source_id in [item for item in table if item not in live_ids]:
                del table[source_id]
        now = self.now()
        due = sorted(
            (due_at, source.id)
            for source in sources
            if (due_at := self._background_discovery_due(source, now, max_age=max_age)) <= now
        )
        if max_age is None:
            self._background_discovery_caught_up = True
        for _due_at, source_id in due:
            stopping = self._background_discovery_stop
            if stopping is not None and stopping.is_set():
                return
            await self._refresh_source_in_background(source_id)

    @staticmethod
    def _background_discovery_eligible(source: ModelHubSourceConfig) -> bool:
        # A native CLI keeps its own catalog.
        return (
            source.supply_channel == "hub"
            and bool(source.credential_ref)
            and source.state.detail_key not in _UNDISCOVERABLE_SOURCE_DETAILS
        )

    def _background_listed_at(self, source: ModelHubSourceConfig) -> datetime | None:
        """The latest successful listing, persisted or process-local."""

        listed = [
            instant for instant in (
                parse_model_hub_timestamp(source.last_discovered_at)
                if source.last_discovered_at else None,
                self._background_discovery_checked.get(source.id),
            )
            if instant is not None
        ]
        return max(listed, default=None)

    def _background_discovery_retry(self, source: ModelHubSourceConfig) -> _DiscoveryRetry | None:
        retry = self._background_discovery_retries.get(source.id)
        if retry is None or retry.identity != source_identity(source):
            return None
        listed_at = self._background_listed_at(source)
        # Any later successful listing, including a manual refresh, resets backoff.
        return None if listed_at is not None and listed_at > retry.failed_at else retry

    def _background_discovery_due(
        self,
        source: ModelHubSourceConfig,
        now: datetime,
        *,
        max_age: timedelta | None = None,
    ) -> datetime:
        retry = self._background_discovery_retry(source)
        if retry is not None:
            return retry.failed_at + retry.delay
        listed_at = self._background_listed_at(source)
        if listed_at is None:
            return now
        period = _background_discovery_period(source.id)
        if not self._background_discovery_caught_up:
            # The first pass catches up every Source past the nominal interval.
            period = min(period, _BACKGROUND_DISCOVERY_INTERVAL)
        if max_age is not None:
            period = min(period, max_age)
        if source_identity(source) in self._stale_listings.get(source.id, {}):
            period = min(period, _STALE_LISTING_AGE)
        return listed_at + period

    async def _refresh_source_in_background(self, source_id: str) -> None:
        """Add newly listed models without touching health or removing anything.

        A listing is not inference evidence, and removals move supply, so Source
        state, removals, and their guards stay with a manual `refresh_source`.
        A stale mark's models are the exception: each leaves if this listing no
        longer names it either, under the identity that refused it, and only
        when its removal moves no route.
        """

        if source_id in self._background_discovery_inflight:
            # The schedule and a viewer's pass never list one Source twice at once.
            return
        self._background_discovery_inflight.add(source_id)
        try:
            await self._refresh_listed_source_in_background(source_id)
        finally:
            self._background_discovery_inflight.discard(source_id)

    async def _refresh_listed_source_in_background(self, source_id: str) -> None:
        config = self.store.load()
        if not config.enabled:
            # Re-read per Source: a runtime stopped mid-pass is not listed again.
            return
        observed = next(
            (item for item in config.sources if item.id == source_id),
            None,
        )
        if observed is None or not self._background_discovery_eligible(observed):
            return
        basis = (source_identity(observed), observed.last_discovered_at)
        marked: frozenset[str] | None = None
        try:
            discovered = await asyncio.wait_for(
                self._discover(observed, start_engine=False),
                timeout=_SOURCE_DISCOVERY_TIMEOUT_SECONDS,
            )
            async with self._mutation_lock:
                previous = self.store.load()
                current = next(
                    (item for item in previous.sources if item.id == source_id),
                    None,
                )
                if current is None or (
                    source_identity(current), current.last_discovered_at,
                ) != basis:
                    # The listing describes a Source another owner has changed
                    # since; the next tick lists the current one.
                    return
                # Read now, not at the start: a refusal that arrived while this
                # listing ran is answered by it.
                marks = self._stale_listings.get(source_id, {})
                for identity in [identity for identity in marks if identity != basis[0]]:
                    # The identity that refused these models is gone, and its refusals with it.
                    del marks[identity]
                marked = marks.get(basis[0])
                updated = self._clone_config(previous)
                source = self._source(updated, source_id)
                overrides = self._merge_discovered_models(source, discovered)
                disowned = (marked or frozenset()) - {
                    canonical_model_id(model.id) for model in discovered
                }
                if disowned:
                    _drop_discovered_models(source, disowned)
                if self._inventory_fingerprint(source) != self._inventory_fingerprint(current):
                    if not self._moves_supply(previous, updated):
                        await self._commit_synced(previous, updated)
                        self._record_reasoning_tier_overrides(source, overrides)
                    else:
                        # A disowned row may leave on its own when the rest of
                        # this listing has to wait, each one whose own removal
                        # moves no route: one that guards a route is no reason
                        # to keep another that does not (#2777).
                        dropped = self._clone_config(previous)
                        leaving = False
                        for model_id in sorted(disowned):
                            trial = self._clone_config(dropped)
                            if _drop_discovered_models(self._source(trial, source_id), {model_id}) and (
                                not self._moves_supply(previous, trial)
                            ):
                                dropped, leaving = trial, True
                        if leaving:
                            await self._commit_synced(previous, dropped)
                        logger.info(
                            "Model Hub background discovery left a supply change "
                            "for Source %s to a manual refresh",
                            source_id,
                        )
        except Exception as exc:
            self._record_background_discovery_failure(observed, exc)
            return
        self._background_discovery_checked[source_id] = self.now()
        marks = self._stale_listings.get(source_id)
        if marks is None:
            return
        if marked is not None and marks.get(basis[0]) is marked:
            # This listing answered the mark; a later refusal marks again.
            del marks[basis[0]]
        if not marks:
            del self._stale_listings[source_id]

    def _moves_supply(self, previous: ModelHubConfig, updated: ModelHubConfig) -> bool:
        """Whether an inventory change removes a route hop or interrupts supply, which only a manual refresh may."""

        return bool(self._inventory_removed_hops(previous, updated) or self._introduced_interruptions(previous, updated))

    def _record_background_discovery_failure(
        self,
        source: ModelHubSourceConfig,
        error: Exception,
    ) -> None:
        previous = self._background_discovery_retry(source)
        delay = (
            _BACKGROUND_DISCOVERY_RETRY_BASE
            if previous is None
            else min(previous.delay * 2, _BACKGROUND_DISCOVERY_RETRY_CAP)
        )
        self._background_discovery_retries[source.id] = _DiscoveryRetry(
            source_identity(source), self.now(), delay,
        )
        # Codes and type names only: engine and upstream errors may carry
        # upstream context.
        logger.warning(
            "Model Hub background discovery failed for Source %s (%s); retrying in %d minutes",
            source.id,
            error.code if isinstance(error, ModelHubError) else type(error).__name__,
            delay.total_seconds() // 60,
        )

    def _merge_discovered_models(
        self,
        source: ModelHubSourceConfig,
        discovered: list[DiscoveredModel],
    ) -> list[tuple[str, Literal["upstream", "catalog"]]]:
        """Admit newly listed models and refresh listed metadata; keep every row."""

        existing = list(source.models)
        manual = [model for model in existing if model.provenance == "manual"]
        overrides = self._apply_discovered_models(source, manual, discovered, allow_empty=True)
        listed = {model.id: model for model in source.models}
        merged = [listed.pop(model.id, model) for model in existing]
        # Existing rows keep their order, so an upstream that reorders its
        # listing changes nothing; new rows follow the active discovered slice.
        insert_at = max(
            (
                index + 1 for index, model in enumerate(existing)
                if model.provenance == "discovered" and not model.retired
            ),
            default=0,
        )
        source.models = [*merged[:insert_at], *listed.values(), *merged[insert_at:]]
        return overrides

    @staticmethod
    def _inventory_fingerprint(source: ModelHubSourceConfig) -> list[dict]:
        # When a model was last listed is not inventory.
        return [
            {key: value for key, value in model.to_payload().items() if key != "discovered_at"}
            for model in source.models
        ]

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
        allowed = {"order", "force", "would_remove_hops", "would_interrupt"}
        if "order" not in payload or set(payload) - allowed or (
            "force" in payload and not isinstance(payload["force"], bool)
        ):
            rejected = sorted(set(payload) - allowed)
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
            removed = self._removed_effective_hops(previous, config, backend=backend)
            interrupted = self._introduced_interruptions(previous, config)
            self._require_guard_plan(
                force=payload.get("force") is True,
                confirmed_remove_hops=payload.get("would_remove_hops"),
                confirmed_interruptions=payload.get("would_interrupt"),
                would_remove_hops=removed,
                would_interrupt=interrupted,
                error="source_in_route_chain" if removed else "source_last_supplier",
            )
            await self._commit_synced(previous, config)
            return self._agent_payload(config, agent)

    def get_agent_sources(self, backend: str) -> dict:
        config = self.store.load()
        return self._agent_payload(
            config, self._agent(config, backend), live_recovery=self.recovery.annotations(config),
        )

    async def read_agent_sources(self, backend: str) -> dict:
        """``get_agent_sources`` for a caller on the controller loop, which owns the recovery state.

        Reading that state may wake its waiters, so it is read here; the config read and the payload are
        built off the loop.
        """
        config = await asyncio.to_thread(self.store.load)
        agent = self._agent(config, backend)
        live_recovery = self.recovery.annotations(config)
        return await asyncio.to_thread(self._agent_payload, config, agent, live_recovery=live_recovery)

    async def set_agent_chain(self, backend: str, model_id: object, payload: object) -> dict:
        if (
            backend not in MODEL_HUB_BACKENDS
            or not isinstance(model_id, str)
            or normalized_model_id(model_id) != model_id
            or not isinstance(payload, dict)
            or set(payload)
            - {"hops", "force", "would_remove_hops", "would_interrupt"}
            or "hops" not in payload
            or ("force" in payload and not isinstance(payload["force"], bool))
        ):
            raise ModelHubError("mapping_target_unavailable", status=409)
        try:
            route = ModelHubRouteConfig.from_payload({"hops": payload["hops"]})
            if any(normalized_model_id(hop["model_id"]) != hop["model_id"] for hop in payload["hops"]):
                raise ValueError("noncanonical model")
        except (TypeError, ValueError):
            raise ModelHubError("mapping_target_unavailable", status=409) from None
        return await self._write_agent_chain(backend, model_id, route, payload)

    def _validate_route_override(
        self,
        config: ModelHubConfig,
        backend: str,
        model_id: object,
        route: ModelHubRouteConfig | None,
    ) -> ModelHubAgentSupplyConfig:
        agent = self._agent(config, backend)
        if agent.mode == "direct":
            raise self._direct_mode_error()
        if not isinstance(model_id, str) or normalized_model_id(model_id) != model_id or (
            model_id not in self._agent_menu_model_ids(agent)
        ):
            raise ModelHubError("mapping_target_unavailable", status=409)
        by_id = {source.id: source for source in config.sources}
        old_pairs = {
            (hop.source_id, hop.model_id)
            for hop in agent.routes.get(model_id, ModelHubRouteConfig()).hops
        }
        for hop in route.hops if route is not None else ():
            source = by_id.get(hop.source_id)
            if source is None or not self._eligible_for_agent(source, backend):
                raise ModelHubError("mapping_target_unavailable", status=409)
            # Existing exact targets keep their load-time identity; only a new
            # source/target identity is subject to new-ID admission.
            persisted_target = (hop.source_id, hop.model_id) in old_pairs or any(
                model.id == hop.model_id for model in source.models
            )
            if not persisted_target and canonical_model_id(hop.model_id) != hop.model_id:
                raise ModelHubError("mapping_target_unavailable", status=409)
            if source_supports_passthrough(source):
                valid = not source_model_retired(source, hop.model_id)
            else:
                valid = (hop.source_id, hop.model_id) in old_pairs or any(
                    model.id == hop.model_id and not model.retired for model in source.models
                )
            if not valid:
                raise ModelHubError("mapping_target_unavailable", status=409)
        return agent

    def preview_agent_chain(self, backend: str, model_id: object, payload: object) -> dict:
        if not isinstance(payload, dict) or set(payload) != {"manual_override"}:
            raise ModelHubError("mapping_target_unavailable", status=409)
        raw = payload["manual_override"]
        try:
            route = ModelHubRouteConfig.from_payload(raw) if raw is not None else None
            if raw is not None and any(normalized_model_id(hop["model_id"]) != hop["model_id"] for hop in raw["hops"]):
                raise ValueError("noncanonical model")
        except (TypeError, ValueError):
            raise ModelHubError("mapping_target_unavailable", status=409) from None
        config = self._clone_config(self.store.load())
        agent = self._validate_route_override(config, backend, model_id, route)
        route = normalized_model_hub_override(route)
        if route is None:
            agent.routes.pop(model_id, None)
        else:
            agent.routes[model_id] = route
        return self._agent_chain(config, backend, model_id)

    async def delete_agent_chain(self, backend: str, model_id: object, payload: object = None) -> dict:
        if payload is None:
            payload = {}
        if not isinstance(payload, dict) or set(payload) - {"force", "would_remove_hops", "would_interrupt"} or (
            "force" in payload and not isinstance(payload["force"], bool)
        ):
            raise ModelHubError("mapping_target_unavailable", status=409)
        return await self._write_agent_chain(backend, model_id, None, payload)

    async def _write_agent_chain(
        self,
        backend: str,
        model_id: object,
        route: ModelHubRouteConfig | None,
        payload: dict,
    ) -> dict:

        async with self._mutation_lock:
            previous = self.store.load()
            config = self._clone_config(previous)
            agent = self._validate_route_override(config, backend, model_id, route)
            route = normalized_model_hub_override(route)
            if route is None:
                agent.routes.pop(model_id, None)
            else:
                agent.routes[model_id] = route
            removed_hops = self._removed_effective_hops(previous, config, backend=backend, model_id=model_id)
            interrupted = self._introduced_interruptions(previous, config)
            if interrupted or (route is None and removed_hops):
                self._require_guard_plan(
                    force=payload.get("force") is True,
                    confirmed_remove_hops=payload.get("would_remove_hops"),
                    confirmed_interruptions=payload.get("would_interrupt"),
                    would_remove_hops=removed_hops,
                    would_interrupt=interrupted,
                    error="source_last_supplier" if interrupted else "source_in_route_chain",
                )
            await self._commit_synced(previous, config)
            return {
                "chain": self._agent_chain(config, backend, model_id),
                "removed_hops": removed_hops,
                "interrupted": interrupted,
            }

    @staticmethod
    def _catalog_model_payload(model: ModelHubBackendModelConfig) -> dict:
        return {
            **model.to_payload(),
            "locked": False,
            "routeable": True,
        }

    def _catalog_models_payload(
        self,
        agent: ModelHubAgentSupplyConfig,
    ) -> list[dict]:
        hidden = self._hidden_retired_model_ids(agent)
        return [self._catalog_model_payload(model) for model in agent.models if model.id not in hidden]

    def _withdrawn_builtin_model_ids(self, backend: BackendName) -> set[str]:
        # Either catalog may withdraw a model; the remote one does so without a
        # release. A stale remote cache schedules the controller-owned refresh,
        # whose completion reconciles the snapshot, so a new tombstone reaches
        # the picker without waiting for a restart. A retired id the current
        # snapshot revives is not withdrawn.
        from vibe.backend_model_catalog import (
            load_bundled_catalog,
            load_cached_remote_catalog,
            retired_backend_model_ids,
            unlisted_retired_backend_model_ids,
        )

        retired = (
            retired_backend_model_ids(backend, load_bundled_catalog())
            | retired_backend_model_ids(backend, load_cached_remote_catalog())
            | unlisted_retired_backend_model_ids(backend)
        )
        if not retired:
            return set()
        return retired - {item["id"] for item in self._current_builtin_models(backend)}

    def _hidden_retired_model_ids(self, agent: ModelHubAgentSupplyConfig) -> set[str]:
        # A retired built-in stays persisted and routeable, so any session,
        # channel, or Agent pin keeps working; it only leaves picker
        # projections. A row a manual route pins or the backend currently
        # requests stays visible.
        withdrawn = self._withdrawn_builtin_model_ids(cast(BackendName, agent.backend))
        if not withdrawn:
            return set()
        requested = self._requested_model(agent)
        return {
            model.id
            for model in agent.models
            if model.origin == "builtin"
            and model.id in withdrawn
            and model.id != requested
            and normalized_model_hub_override(agent.routes.get(model.id)) is None
        }

    def backend_catalog_models(self, backend: str) -> list[dict]:
        if backend not in MODEL_HUB_BACKENDS:
            raise ModelHubError("mapping_target_unavailable", status=404)
        config = self.store.load()
        return self._catalog_models_payload(config.agents[backend])

    def _agent_payload(
        self, config: ModelHubConfig, agent: ModelHubAgentSupplyConfig,
        *, live_recovery: Mapping[str, SourceRecoveryAnnotation] | None = None,
    ) -> dict:
        backend = cast(BackendName, agent.backend)
        hidden = self._hidden_retired_model_ids(agent)
        builtin_models = (
            [model.id for model in agent.models if model.id not in hidden]
            if agent.menu_kind == "fixed"
            else None
        )
        requested_model = self._requested_model(agent)
        unavailable_source_ids = self._unavailable_native_sources(config, backend)
        now = self.now()
        if live_recovery is None:
            live_recovery = self.recovery_annotations(config)
        resolution = resolve_model_hub_turn(
            config,
            backend,
            requested_model,
            now=now,
            unavailable_source_ids=unavailable_source_ids,
            live_recovery=live_recovery,
        )
        menu_models = [model for model in agent.models if model.id not in hidden]
        model_resolutions = {
            model.id: resolve_model_hub_turn(
                config, backend, model.id, now=now, unavailable_source_ids=unavailable_source_ids,
                live_recovery=live_recovery,
            )
            for model in menu_models
        }
        model_supply = [
            {
                "model_id": model_id,
                "chain_length": len(model_resolution.inspected_hops),
                "route_origin": model_resolution.route_origin,
                "has_runnable_hop": bool(model_resolution.candidate_hops),
            }
            for model_id, model_resolution in model_resolutions.items()
        ]
        # Only Hub turns reach the evidence; a Direct Agent's menu is the backend's own.
        effort_menus = {
            model.id: (
                self._effort_menu(backend, model, model_resolutions[model.id], now)
                if agent.mode == "hub"
                else {"omitted": [], "not_on": {}}
            )
            for model in menu_models
        }
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
                requested = self._menu_model_for_selection(
                    agent, str(pinned_model or "").strip()
                )
                named_resolution = resolve_model_hub_turn(
                    config,
                    backend,
                    requested,
                    now=now,
                    unavailable_source_ids=unavailable_source_ids,
                    live_recovery=live_recovery,
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
            "catalog_models": [
                {**model, "effort_menu": effort_menus[model["id"]]} for model in self._catalog_models_payload(agent)
            ],
            "builtin_models": builtin_models,
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
                (
                    catalog_wire_efforts(model.id, model.reasoning_efforts)
                    if model.reasoning_efforts_source == "catalog"
                    else model.reasoning_efforts
                ),
            )
            if display_name is None and proposed_name is not None:
                display_name = proposed_name
            for effort in proposed_efforts:
                if effort not in reasoning_efforts:
                    reasoning_efforts.append(effort)
        return suppliers, display_name, reasoning_efforts

    def _models_dev_descriptions(self, model_ids: list[str], *, fetch_catalog: bool = True) -> dict[str, dict]:
        """Exact models.dev matches for provider candidates; empty when unknown.

        The description is optional: an unreadable catalog leaves every
        candidate as its suppliers describe it rather than failing the picker.
        """

        if not model_ids:
            return {}
        from vibe.models_dev_catalog import cached_models_dev_catalog, exact_models_dev_matches

        try:
            catalog = self.models_dev_catalog() if fetch_catalog else cached_models_dev_catalog()
            return exact_models_dev_matches(model_ids, dict(catalog))
        except Exception as exc:  # noqa: BLE001 - optional metadata never fails a read
            logger.info("Model Hub candidates have no models.dev metadata: %s", type(exc).__name__)
            return {}

    def _provider_candidate(
        self,
        config: ModelHubConfig,
        backend: BackendName,
        model_id: str,
        match: Mapping[str, Any] | None,
    ) -> tuple[ModelHubBackendModelConfig, list[dict], tuple[str, ...]] | None:
        """Build the catalog row a provider-supplied model is added as.

        Returns the admitted row, its suppliers in Source order, and the
        models.dev fields that filled it, or None when the id is not admissible.
        """

        from vibe.models_dev_catalog import native_protocol_for_model_id

        suppliers, display_name, reasoning_efforts = self._candidate_suppliers(
            config,
            backend,
            model_id,
        )
        # Suppliers speak first, models.dev fills what they left unsaid, and
        # an unstated ladder falls back to the tiers the backend's request
        # protocol accepts, unless models.dev says the model cannot reason.
        # Vibey has no native protocol pin and must not receive guessed tiers.
        enrichment = (
            {field: match[field] for field in _MODELS_DEV_CANDIDATE_FIELDS}
            if match is not None
            else {}
        )
        if reasoning_efforts and enrichment.get("supports_reasoning") is False:
            # A supplier's ladder outranks the catalog's flag, and a false
            # flag would suppress that ladder at launch.
            enrichment["supports_reasoning"] = None
        display_name = display_name or (match or {}).get("display_name")
        if not reasoning_efforts and match is not None:
            reasoning_efforts = list(match["reasoning_efforts"])
        if backend != "vibey" and not reasoning_efforts and enrichment.get("supports_reasoning") is not False:
            request_protocol = _FIXED_BACKEND_PROTOCOLS.get(
                backend
            ) or native_protocol_for_model_id(model_id)
            reasoning_efforts = list(PROTOCOL_REASONING_EFFORT_DEFAULTS[request_protocol])
        admitted = admissible_backend_model(
            backend,
            model_id,
            {
                "origin": "provider",
                "display_name": display_name,
                "reasoning_efforts": reasoning_efforts,
                **enrichment,
                **(
                    {"native_protocol": native_protocol_for_model_id(model_id)}
                    if backend == "opencode"
                    else {}
                ),
            },
            claude_builtin_ids=_builtin_model_ids("claude"),
        )
        if admitted is None:
            return None
        return admitted, suppliers, tuple(enrichment)

    def agent_model_candidates(self, backend: str) -> dict:
        agent_backend = cast(BackendName, backend)
        config = self.store.load()
        agent = self._agent(config, backend)
        from vibe.models_dev_catalog import native_protocol_for_model_id

        def protocol_payload(model_id: str) -> dict[str, str]:
            if backend != "opencode":
                return {}
            return {"native_protocol": native_protocol_for_model_id(model_id)}

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
                    **protocol_payload(model_id),
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
                    **protocol_payload(admitted.id),
                }
            )

        provider_ids: list[str] = []
        # A source inventory (a native subscription lists the full fixed menu)
        # never re-offers a withdrawn built-in, whether or not a row persists;
        # a still-visible pinned row is already in the menu.
        withdrawn = self._withdrawn_builtin_model_ids(agent_backend)
        source_by_id = {source.id: source for source in config.sources}
        for source_id in agent.sources.order:
            source = source_by_id.get(source_id)
            if source is None or not self._eligible_for_agent(source, backend):
                continue
            for model in source.models:
                if model.retired:
                    continue
                candidate_id = model.id
                if (
                    candidate_id in withdrawn
                    or candidate_id in menu_ids
                    or candidate_id in builtin_ids
                    or candidate_id in provider_ids
                ):
                    continue
                provider_ids.append(candidate_id)

        providers = []
        described = self._models_dev_descriptions(provider_ids)
        for model_id in provider_ids:
            candidate = self._provider_candidate(
                config,
                agent_backend,
                model_id,
                described.get(model_id),
            )
            if candidate is None:
                continue
            admitted, suppliers, enriched_fields = candidate
            providers.append(
                {
                    "id": admitted.id,
                    "display_name": admitted.display_name,
                    "reasoning_efforts": admitted.reasoning_efforts,
                    "suppliers": suppliers,
                    "origin": "provider",
                    **{
                        field: getattr(admitted, field)
                        for field in enriched_fields
                    },
                    **protocol_payload(admitted.id),
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
                    **protocol_payload(model["id"]),
                }
            )
        return {"builtin": builtin, "providers": providers, "in_list": in_list}

    def list_agents(self) -> list[dict]:
        config = self.store.load()
        live_recovery = self.recovery.annotations(config)
        return [
            self._agent_payload(config, config.agents[backend], live_recovery=live_recovery)
            for backend in MODEL_HUB_BACKENDS
        ]

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
        if backend == "vibey":
            return False
        if self.cli_present_override is None:
            return False
        try:
            return bool(self.cli_present_override(backend))
        except Exception:
            logger.warning("Model Hub CLI presence probe failed for %s", backend, exc_info=True)
            return False

    async def _native_config_blocks_launch(self, config: ModelHubConfig, backend: str) -> bool:
        # Native auth stays beside the Hub, but a config the CLI cannot parse
        # fails every launch.
        completed = self.migration_journal.completed() or {}
        available = await asyncio.to_thread(
            scan_native_configs, config,
            mask_credential=_mask_credential,
            home=self.migration_home,
            validate_base_url=_validated_base_url,
            legacy_auth=(
                self.store.native_auth_snapshot((backend,))
                if isinstance(self.store, V2ModelHubConfigStore) else None
            ),
            project_roots=self.migration_project_roots(),
            clean_native_stores=completed.get("clean_native_stores"),
            retained_native_ids=completed.get("retained_native_ids"),
        )
        return any(item.backend == backend and item.config_blocker for item in available)

    def _carry_agent_selections(
        self,
        config: ModelHubConfig,
        backend: BackendName,
        *,
        native_sources: Collection[tuple[str, str]] = (),
        fetch_catalog: bool = True,
    ) -> None:
        """Give each Agent's model a Gateway menu row when a Source serves it.

        Called after confirmed migration or as a backend enters Gateway mode.
        Direct mode never reads the Gateway menu, so an Agent there keeps a
        model its CLI offered, and an
        OpenCode one spells it with its own provider (``openai/gpt-5.6-sol``).
        The row added is the one the picker adds for a provider model, only
        when a Source in this backend's default routing lists it. The Agent's
        selection itself is untouched, so switching back to Direct still runs
        it. A row the user removed stays removed, and a selection nothing
        serves stays visible as an Agent that needs attention. During migration,
        a static Source carried from that selection's native provider can instead
        retain it by passthrough, without claiming an upstream inventory.
        """

        if self.named_agents_override is None:
            return
        agent = config.agents[backend]
        source_by_id = {source.id: source for source in config.sources}
        carried: list[str] = []
        for _name, selected in self.named_agents_override(backend):
            selected = str(selected or "").strip()
            menu_ids = {model.id for model in agent.models}
            if not selected or self._menu_model_for_selection(agent, selected) in menu_ids:
                continue
            if backend == "opencode" and native_sources:
                provider, separator, model = selected.partition("/")
                if not separator or not model or not any(
                    native_provider == provider for native_provider, _ in native_sources
                ):
                    # A same-named inventory entry from a different provider
                    # does not consent to migrating this Direct selection.
                    continue
            candidates = (
                opencode_menu_model_candidates(selected)
                if backend == "opencode"
                else (selected,)
            )
            served = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate not in agent.removed_model_ids
                    and candidate not in carried
                    and self._matching_menu_model_hops(config, backend, candidate)
                ),
                None,
            )
            if served is None and native_sources:
                provider, separator, model = selected.partition("/")
                native_model = model if backend == "opencode" and separator else selected
                if (
                    (backend != "opencode" or (provider and separator and model))
                    and native_model not in agent.removed_model_ids
                    and native_model not in carried
                    and any(
                        (backend != "opencode" or native_provider == provider)
                        and source_id in agent.sources.order
                        and (source := source_by_id.get(source_id)) is not None
                        and self._eligible_for_agent(source, backend)
                        and source_supports_passthrough(source)
                        and not source_model_retired(source, native_model)
                        for native_provider, source_id in native_sources
                    )
                ):
                    served = native_model
            if served is not None:
                carried.append(served)
        if not carried:
            return
        described = self._models_dev_descriptions(carried, fetch_catalog=fetch_catalog)
        for model_id in carried:
            candidate = self._provider_candidate(
                config,
                backend,
                model_id,
                described.get(model_id),
            )
            if candidate is not None:
                agent.models.append(candidate[0])
        if backend == "opencode":
            agent.menu = ModelHubMenuConfig(
                view=agent.menu.view if agent.menu else "featured",
                checked=[model.id for model in agent.models],
            )

    async def _seed_vibey(self, config: ModelHubConfig, *, fetch_catalog: bool = True) -> bool:
        """Give a pending Vibey entry its starting supply, in place; whether it took.

        Its Sources are every existing one this backend may use, placed as a
        newly created Source would be. Its models are the built-in Agents'
        selections, in order, that one of those Sources lists, each added as the
        picker adds a provider model. With none qualifying, Vibey starts on one
        model the Sources list (``_vibey_starting_model``), so a new install's
        default Agent can run once setup places a Source; a model nothing can
        describe leaves the list empty. The seed counts only once it has placed a
        Source: until then the entry stays pending, so the first Source the user
        adds still brings every Source and the starting models with it. Each
        mutation that adds Sources seeds after its Sources are final, so one
        that adds several seeds from all of them.
        """

        agent = config.agents["vibey"]
        if not any(self._eligible_for_agent(source, "vibey") for source in config.sources):
            return False
        # Seeded rows keep the models.dev metadata they are written with.
        # Migration uses only the cached copy so moving a saved key never waits
        # on optional network metadata; ordinary background backfill still runs.
        if fetch_catalog:
            await asyncio.to_thread(self._ensure_models_dev_copy)
        config.vibey_supply_pending = False
        for source in config.sources:
            self._apply_source_placement(config, source, ("vibey",))
        # A starting model is one a placed Source's own inventory lists; a
        # route, passthrough included, is no evidence.
        listed = {
            model.id
            for source in config.sources
            if source.id in agent.sources.order
            for model in source.models
            if not model.retired
        }
        selections = self.builtin_agent_models_override() if self.builtin_agent_models_override else ()
        model_ids: list[str] = []
        for backend, selected in selections:
            served = next(
                (
                    candidate
                    for candidate in (
                        opencode_menu_model_candidates(selected)
                        if backend == "opencode"
                        else (selected,)
                    )
                    if candidate in listed
                ),
                None,
            )
            if served is not None and served not in model_ids:
                model_ids.append(served)
        described = self._models_dev_descriptions(model_ids, fetch_catalog=fetch_catalog)
        rows = []
        for model_id in model_ids:
            match = described.get(model_id)
            # A selection is a person's choice for a native Agent: only a description
            # that rules out an Agent's turn keeps it from starting Vibey.
            if match is not None and _described_as_unfit_for_agent_turns(match):
                continue
            candidate = self._provider_candidate(config, "vibey", model_id, match)
            if candidate is not None:
                rows.append(candidate[0])
        if not rows:
            starting = self._vibey_starting_model(config, fetch_catalog=fetch_catalog)
            rows = [starting] if starting is not None else []
        agent.models = rows
        return True

    def _vibey_starting_model(
        self, config: ModelHubConfig, *, fetch_catalog: bool = True,
    ) -> ModelHubBackendModelConfig | None:
        """The one model a seed with no qualifying selection starts Vibey on, or ``None``.

        The first model, in Vibey's Source order and then each Source's own
        inventory order, that models.dev describes (by the exact match the
        picker's candidates use) as taking tool calls and producing text: the
        least an Agent's turn needs. A model models.dev does not know is never
        guessed at, so an inventory it cannot describe starts Vibey with none.
        """

        source_by_id = {source.id: source for source in config.sources}
        listed = list(
            dict.fromkeys(
                model.id
                for source_id in config.agents["vibey"].sources.order
                if (source := source_by_id.get(source_id)) is not None
                for model in source.models
                if not model.retired
            )
        )
        described = self._models_dev_descriptions(listed, fetch_catalog=fetch_catalog)
        for model_id in listed:
            match = described.get(model_id)
            if match is None or not _described_as_fit_for_agent_turns(match):
                continue
            candidate = self._provider_candidate(config, "vibey", model_id, match)
            if candidate is not None:
                return candidate[0]
        return None

    async def _announce_vibey_seed(self) -> None:
        """Announce a committed seed as the Vibey catalog change it is.

        Like every catalog mutation it ends in ``_refresh_backend_catalog``,
        whose owner reconciles the built-in Vibey Agent's model. The seed is already
        committed, so a failed announcement is logged; the next start reconciles.
        """

        try:
            await self._refresh_backend_catalog("vibey")
        except Exception:  # noqa: BLE001 - the committed supply stands
            logger.warning("Model Hub: Vibey's seeded catalog was not announced", exc_info=True)

    async def seed_vibey_supply(self) -> list[str]:
        """Seed a pending Vibey entry from the Sources that existed at startup.

        Source creation seeds a pending entry in its own mutation; this covers
        Sources that predate Vibey. Returns the seeded model ids.
        """

        if not self.store.load().vibey_supply_pending:
            return []
        # Seeded rows are written once and then kept, so a first models.dev copy
        # is worth one bounded foreground fetch, off the loop and outside the
        # lock. Taken even with no Source yet, so a fresh install's first
        # Source usually finds it.
        await asyncio.to_thread(self._ensure_models_dev_copy)
        async with self._mutation_lock:
            previous = self.store.load()
            if not previous.vibey_supply_pending:
                return []
            config = self._clone_config(previous)
            if not await self._seed_vibey(config):
                return []
            try:
                # An engine that is not up must not undo the seed: the config is
                # saved first, and the engine takes it on its next demand.
                await self._commit_synced(previous, config, rollback_on_sync_failure=False)
            except ModelHubError:
                if self.store.load().vibey_supply_pending:
                    raise
        await self._announce_vibey_seed()
        return [model.id for model in config.agents["vibey"].models]

    async def start_vibey_without_models(self) -> list[str]:
        """Start a seeded Vibey whose catalog is still empty on its starting model; the ids it took.

        A seed that found nothing describable leaves the catalog empty. The snapshot
        reconcile calls this, when a models.dev copy arrives and on its cadence, and
        only while Vibey's built-in Agent has no model, so whatever later makes a
        listed model describable (the copy, or a Source added since) starts a Vibey
        nothing was ever chosen for. A catalog a person emptied records its removals,
        so it is theirs and stays empty.
        """

        async with self._mutation_lock:
            previous = self.store.load()
            vibey = previous.agents["vibey"]
            if previous.vibey_supply_pending or vibey.models or vibey.removed_model_ids:
                return []
            starting = self._vibey_starting_model(previous)
            if starting is None:
                return []
            config = self._clone_config(previous)
            config.agents["vibey"].models = [starting]
            try:
                # As for the seed, an engine that is not up must not undo the start: the
                # config is saved first, and the Agent still has to hear about it.
                await self._commit_synced(previous, config, rollback_on_sync_failure=False)
            except ModelHubError:
                if not self.store.load().agents["vibey"].models:
                    raise
        await self._announce_vibey_seed()
        return [starting.id]

    def _ensure_models_dev_copy(self) -> None:
        """Fetch models.dev in the foreground, bounded, when no copy is cached.

        A foreground fetch already running is shared through the catalog's cache
        lock; a background refresh is not waited for. A failure leaves no copy.
        """

        from vibe.models_dev_catalog import load_models_dev_catalog

        try:
            if not self.models_dev_catalog():
                load_models_dev_catalog()
        except Exception as exc:  # noqa: BLE001 - optional metadata never fails a seed
            logger.info("Vibey seed has no models.dev metadata: %s", type(exc).__name__)

    @staticmethod
    def _rows_lacking_models_dev_limits(
        config: ModelHubConfig,
    ) -> list[tuple[BackendName, ModelHubBackendModelConfig]]:
        return [
            (backend, model)
            for backend in MODEL_HUB_BACKENDS
            for model in config.agents[backend].models
            if model.origin == "provider"
            and model.models_dev_id is None
            and (model.context_window is None or model.max_output_tokens is None)
        ]

    async def backfill_models_dev_limits(self) -> list[BackendName]:
        """Give provider rows written with no models.dev copy cached the limits one states.

        A provider row takes models.dev's description as it is written (see
        ``_provider_candidate``). Written before any copy was cached, by the
        picker or a seed, it has none, and an Agent budgets it with default
        limits. Once a copy names its id exactly, the row gets each limit it
        lacks and the ``models_dev_id`` they came from. A limit already set
        stands, and a row models.dev has described is not described again, so
        a limit its user cleared stays cleared. Built-in rows are their CLI's
        description and manual rows their user's; neither is filled. Returns
        the backends whose catalog changed.
        """

        wanted = {model.id for _backend, model in self._rows_lacking_models_dev_limits(self.store.load())}
        if not wanted:
            return []
        # The copy is parsed off the loop and outside the lock; the rows are
        # read again under it.
        described = await asyncio.to_thread(self._models_dev_descriptions, sorted(wanted))
        if not described:
            return []
        async with self._mutation_lock:
            if not self._reconcile_store_writable():
                return []
            previous = self.store.load()
            config = self._clone_config(previous)
            changed: list[BackendName] = []
            for backend, model in self._rows_lacking_models_dev_limits(config):
                match = described.get(model.id)
                if match is None:
                    continue
                filled = False
                for field in ("context_window", "max_output_tokens"):
                    if getattr(model, field) is None and match[field] is not None:
                        setattr(model, field, match[field])
                        filled = True
                if filled:
                    model.models_dev_id = match["models_dev_id"]
                    if backend not in changed:
                        changed.append(backend)
            if not changed:
                return []
            await self._commit_synced(previous, config)
            for backend in changed:
                await self._refresh_backend_catalog(backend)
            return changed

    def hosted_evidence(self) -> HostedEvidence:
        """A reader over the evidence that applies the current wire stamp; memory only. Take one per resolution."""

        return HostedEvidence(self.capability_evidence, self.engine_stamp, self.avibe_release)

    def hosted_tools_status(self) -> dict:
        """Models → Tools: each tool's settings, saved and effective, and what real use showed."""

        return {
            "hosted_tools": hosted_tools_status(
                self.store.load(),
                evidence=self.hosted_evidence(),
                spec_for=hosted_tool_spec,
                image_price=self._image_price,
                search_trial_pending=self.search_reenable_trial_pending,
                search_cooldown_until=self._search_visible_cooldown,
            )
        }

    def _image_price(self, source: ModelHubSourceConfig, spec: ImageModelSpec) -> Optional[dict]:
        """One image's price for the Tools card: the vendor's list price, else an estimate, else None (never zero).

        Only an unqualified list price is one: a price for one size or quality would read as every image's, and the
        card names neither. The estimate values the overlay's ``tokens_per_image`` at the model's output token rate,
        through the one pricing owner.
        """

        listed = next((entry.price for entry in spec.prices if entry.size is None and entry.quality is None), None)
        if listed is not None:
            return {"usd": round(listed.usd_per_1k / 1000, 6), "basis": "list"}
        if spec.tokens_per_image is None:
            return None
        cost = self.hosted_call_cost(
            replace(source, billing="metered"),
            spec.id,
            (),
            usage=ProtocolUsageReport.of(input_tokens=0, cached_input_tokens=0, output_tokens=spec.tokens_per_image),
            counts={},
        )
        if cost is None or not cost.usd:
            # An estimate of zero means the model's rate is unknown, never that an image is free.
            return None
        return {"usd": cost.usd, "basis": "estimated"}

    async def set_hosted_tool(self, tool: str, payload: object) -> dict:
        """Change one hosted tool's settings, field by field; it applies from the next turn."""

        async with self._mutation_lock:
            previous = self.store.load()
            try:
                setting = apply_hosted_tool_put(previous, tool, payload)
            except HostedToolSettingRefused as refused:
                raise ModelHubError(refused.code, status=refused.status) from None
            if previous.hosted_tools.get(tool) != setting:
                config = self._clone_config(previous)
                config.hosted_tools[tool] = setting
                await self._commit_synced(previous, config)
        return await asyncio.to_thread(self.hosted_tools_status)

    async def clear_hosted_refusals(self, tool: str, payload: object) -> dict:
        """Clear a tool's refusals on one Source (and model), or on every Source; it applies from the next request.

        Persisted before it takes effect: a failed write changes nothing, so what the user is shown holds after a
        restart.
        """

        try:
            matches = clear_request(tool, payload)
        except HostedToolSettingRefused as refused:
            raise ModelHubError(refused.code, status=refused.status) from None
        async with self._mutation_lock:
            await self._clear_evidence(self.store.load(), matches)
        return await asyncio.to_thread(self.hosted_tools_status)

    async def clear_source_evidence(self, source_id: str, payload: object) -> dict:
        """Clear one effort refusal of a Source's model (capability model §5); answers the Source."""

        async with self._mutation_lock:
            config = self.store.load()
            source = next((item for item in config.sources if item.id == source_id), None)
            if source is None:
                raise ModelHubError("source_not_found", status=404)
            if not isinstance(payload, dict) or set(payload) != {"model_id", "feature"}:
                raise ModelHubError("invalid_parameter", status=400)
            model_id = payload["model_id"]
            try:
                feature = EvidenceFeature.from_payload(payload["feature"])
            except (TypeError, ValueError):
                raise ModelHubError("invalid_parameter", status=400) from None
            if not is_model_hub_model_id(model_id) or feature.kind != "effort":
                raise ModelHubError("invalid_parameter", status=400)
            await self._clear_evidence(
                config,
                lambda record: (record.source_id, record.model_id, record.feature) == (source_id, model_id, feature),
            )
            return {"source": self._source_payload(source, config)}

    async def _clear_evidence(self, config: ModelHubConfig, matches: Callable[[EvidenceRecord], bool]) -> None:
        """Clear under the Source mutation lock, so the identities it keeps are the ones the config holds."""

        try:
            await asyncio.to_thread(self.capability_evidence.clear, matches, live=lambda: config.sources)
        except OSError:
            logger.warning("Model Hub could not persist an evidence clear; nothing changed", exc_info=True)
            raise ModelHubError("state_write_failed", status=500) from None

    async def record_hosted_refusals(
        self, source: ModelHubSourceConfig, refusals: Iterable[tuple[str, EvidenceFeature, str]],
    ) -> None:
        """Record refusals no upstream narrow rule writes (an executor model, an image endpoint); memory first.

        Each is ``(model_id, feature, detail)`` under the current wire stamp. One call writes the batch, so sibling
        image models of one endpoint land together. A failed file write is logged; memory keeps them.
        """

        evidence = self.hosted_evidence()
        records = [
            EvidenceRecord(
                source_id=source.id,
                model_id=model_id,
                feature=feature,
                status="unsupported",
                observed_at=self.now(),
                wire=evidence.wire_for(source, model_id),
                source_identity=identity_digest(source),
                detail=detail[:64],
            )
            for model_id, feature, detail in refusals
        ]
        if records:
            await asyncio.to_thread(
                self.capability_evidence.write_refusals,
                records,
                live=lambda: self.store.load().sources,
                wire_for=evidence.wire_for,
            )

    async def record_hosted_success(
        self, source: ModelHubSourceConfig, model_id: str, tool: str, mode: str,
    ) -> None:
        """Record a real success of a hosted tool (display only): the latest projection at once, the file hourly."""

        record = EvidenceRecord(
            source_id=source.id,
            model_id=model_id,
            feature=EvidenceFeature.hosted(tool, "call"),
            status="verified",
            observed_at=self.now(),
            wire=self.hosted_evidence().wire_for(source, model_id),
            source_identity=identity_digest(source),
            mode=mode,  # type: ignore[arg-type]
        )
        await asyncio.to_thread(
            self.capability_evidence.note_success, record, live=lambda: self.store.load().sources, now=record.observed_at,
        )

    def _effort_refused(self, source: ModelHubSourceConfig, model_id: str, request: Any) -> bool:
        """Whether this hop has negative evidence for the effort the request sends.

        Efforts are forwarded unchanged; a hop known to refuse one is not a
        candidate for this request (capability model §3.4). Every place that
        decides on candidates asks this one predicate, so the resolve loop and
        the recovery loop never disagree. Memory-only and lock-free: the store
        read its file at startup.
        """

        return self._refuses_effort(
            source, model_id, wire_effort(request, getattr(request, "protocol", "")), self.now(),
        )

    def _refuses_effort(
        self, source: ModelHubSourceConfig, model_id: str, token: Optional[str], now: datetime
    ) -> bool:
        """Whether this hop has negative evidence for the effort token a request sends; None sends none."""

        feature = EvidenceFeature.effort_or_none(token)
        if feature is None:
            return False
        return self.hosted_evidence().unsupported(source, model_id, feature)

    def _effort_menu(
        self, backend: BackendName, model: ModelHubBackendModelConfig, resolution: ModelHubTurnResolution, now: datetime
    ) -> dict:
        """Which of this menu model's efforts routing refuses now, in menu vocabulary (capability model §3.4).

        A tier is ``omitted`` when a request at it would end in
        ``effort_unsupported``: every hop the route would try or wait for refuses
        the token the backend sends for it, as ``_effort_refused`` decides for a
        request. A tier refused on some of those hops only is ``not_on`` their
        Sources.
        """

        hops = list(
            {
                (hop.source.id, hop.model_id): hop
                for hop in (*resolution.candidate_hops, *resolution.self_healing_hops(None))
                if hop.source is not None and hop.model_id is not None
            }.values()
        )
        omitted: list[str] = []
        not_on: dict[str, list[str]] = {}
        if not hops:
            return {"omitted": omitted, "not_on": not_on}
        # One request carries one token to every hop it may try: the one the backend writes for the hop it builds
        # the request on, the first. ``_effort_refused`` reads that same token from the request.
        primary = hops[0]
        for effort in model.reasoning_efforts:
            token = _sent_effort(backend, primary.source, primary.model_id, effort)
            refusing = [hop.source.id for hop in hops if self._refuses_effort(hop.source, hop.model_id, token, now)]
            if refusing and len(refusing) == len(hops):
                omitted.append(effort)
            elif refusing:
                not_on[effort] = list(dict.fromkeys(refusing))
        return {"omitted": omitted, "not_on": not_on}

    def _effort_capable_hop_may_recover(
        self, backend: str, model_id: str, supply_channel: Any, request: Any
    ) -> bool:
        """Whether a temporarily blocked hop of this route does not refuse the request's effort."""

        resolution = self._invocation_resolution(
            self.store.load(), cast(BackendName, backend), model_id, supply_channel,
        )
        return any(
            hop.source is not None
            and hop.model_id is not None
            and not self._effort_refused(hop.source, hop.model_id, request)
            for hop in resolution.self_healing_hops(supply_channel)
        )

    async def _learn_capability_evidence(
        self, source: ModelHubSourceConfig, model_id: str, request: Any, outcome: RawCallOutcome
    ) -> LearnedEvidence | None:
        """Record the one feature a 400 before output names, through the narrow rules; return it."""

        if outcome.rejection is None:
            return None
        learned = learn_from_rejection(
            request,
            outcome.rejection,
            request_protocol=getattr(request, "protocol", ""),
            source=source,
            model_id=model_id,
            spec_for=hosted_tool_spec,
        )
        if learned is None:
            return None
        evidence = self.hosted_evidence()
        # Routing state: memory takes it before the re-render that follows, even if the file write fails. The store
        # drops it if the Source or the model's rendering changed while the request ran.
        await asyncio.to_thread(
            self.capability_evidence.write_refusal,
            EvidenceRecord(
                source_id=source.id,
                model_id=model_id,
                feature=learned.feature,
                status="unsupported",
                observed_at=self.now(),
                wire=evidence.wire_for(source, model_id),
                source_identity=identity_digest(source),
                detail=learned.detail,
            ),
            live=lambda: self.store.load().sources,
            wire_for=evidence.wire_for,
        )
        return learned

    def _admits_hosted(self, content: HostedContent, source: ModelHubSourceConfig, model_id: str) -> bool:
        """Whether this hop runs a request's hosted content with current positive evidence; memory only."""

        return admits(
            content,
            source=source,
            model_id=model_id,
            spec_for=hosted_tool_spec,
            evidence=self.hosted_evidence(),
        )

    def hosted_call_cost(
        self,
        source: ModelHubSourceConfig,
        model_id: str,
        tools: Iterable[str],
        *,
        usage: Optional[ProtocolUsageReport],
        counts: Mapping[str, int],
        reported_usd: Optional[float] = None,
    ) -> Optional[HostedCost]:
        """The cost of one hosted response served by ``source``/``model_id``; None when unknown, never zero.

        Vibey's ``hosted_summary`` and the gateway's ``ProxyExecution`` both ask
        here, so only the pricing module turns usage into money. Memory-only:
        the token price comes from the table the usage writer already holds, so
        a response that ends before the first table is read is simply unpriced.
        """

        prices = self.usage_writer.held_prices
        priced = hosted_response_cost(
            billing=source.billing,
            reported_usd=reported_usd,
            model_price=prices.price(model_id) if prices is not None else None,
            usage=usage,
            tool_prices={
                tool: (spec.price if (spec := hosted_tool_spec(source.vendor, source.protocol, model_id, tool)) else None)
                for tool in tools
            },
            counts=counts,
        )
        if priced is None:
            return None
        basis, usd = priced
        return HostedCost(basis=basis, usd=None if usd is None else round(usd, 6))

    def context_price_steps(self, model_id: str) -> tuple[int, ...]:
        """Where ``model_id``'s list price steps up with its context (``pricing.context_price_steps``), ascending.

        Memory-only, like ``hosted_call_cost``: read from the table the usage writer already holds, so a hop resolved
        before the first table is read, or for a model with no price, reports none.
        """

        prices = self.usage_writer.held_prices
        return context_price_steps(prices.price(model_id)) if prices is not None else ()

    def _admitted_executor(self, slot: ExecutorSlot, tool: str) -> ExecutorCandidate | None:
        """The chain's candidate as the current snapshot admits it, else None (``vibey-hosted-tools.md`` §6).

        The turn froze the order, not the Sources' runtime: a Source that is
        removed, no longer runs the tool on its executor model, or has refused
        it since is passed over, and so is one whose identity (base URL,
        credential, ...) changed since the chain was built. It reads the config,
        which grows with use: call it off the controller loop.
        """

        config = self.store.load()
        source = next((item for item in config.sources if item.id == slot.source_id), None)
        if source is None or identity_digest(source) != slot.identity:
            return None
        # The frozen model while it stays listed and runnable, even if the Source now lists one its vendor prefers.
        return executor_candidate(source, slot.model_id, tool, evidence=self.hosted_evidence(), spec_for=hosted_tool_spec)

    def _search_visible_cooldown(self, source_id: str, service_id: str) -> str | None:
        config = self.store.load()
        source = next((item for item in config.sources if item.id == source_id), None)
        if source is None:
            return None
        until = self._service_cooldowns.until(source, service_id, self.now())
        return utc_text(until) if until is not None and (until - self.now()).total_seconds() > 60 else None

    async def clear_service_cooldown(self, source_id: str, service_id: str | None = None, payload: object = None) -> dict:
        if payload not in (None, {}):
            raise ModelHubError("invalid_parameter", status=400)
        async with self._mutation_lock:
            config = await asyncio.to_thread(self.store.load)
            source = self._source(config, source_id)
            service = model_hub_search_service(source)
            if service is None or (service_id is not None and service["service_id"] != service_id):
                raise ModelHubError("source_not_found", status=404)
            self._service_cooldowns.advance(source, service["service_id"], clear=True)
        return {}

    async def _run_search_chain(self, call: ExecutorCall, turn: TurnHostedTools, *, say: Callable[..., str]) -> dict:
        """Run the service/model chain; all response decoding and mapping stay off the loop."""
        return await SearchChain(self, call, turn, say).run()

    async def run_hosted_tool(
        self,
        tool: str,
        payload: object,
        *,
        turn: TurnHostedTools | None,
        language: str = "en",
    ) -> dict:
        """Run one proxied search on its turn's executor chain (``vibey-hosted-tools.md`` §6).

        ``turn`` is what the caller's live turn froze; a tool outside it is
        refused, unrun. The chain tries the turn's executors in order, each
        admitted on the snapshot it is called on, within one deadline. It moves
        on after a failure normal routing fails over from, a narrow-rule
        refusal, a refusal of the executor model, or a failure mid-response;
        any other outcome ends it. Every candidate called is metered on its own
        Source. A proxy call never changes Source health: it is a tool call,
        not the turn's model hop.
        """

        def say(key: str, **params: str) -> str:
            return i18n_t(key, language, **params)

        try:
            # Validation is proportional to the request: off the controller loop.
            call = await asyncio.to_thread(parse_call, tool, payload)
        except ExecutorCallRefused as refused:
            raise ModelHubError(refused.code, status=refused.status) from None
        if turn is None or tool not in turn.tools:
            raise ModelHubError("tool_not_enabled", status=409)
        if tool == "web_search" and turn.prefer_native is not None:
            return await self._run_search_chain(call, turn, say=say)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + call_timeout(tool)
        attempts: list[_ExecutorAttempt] = []
        for slot in turn.executors.get(tool, ()):
            if loop.time() >= deadline:
                break
            attempt = await self._executor_attempt(call, slot, deadline=deadline, say=say)
            if attempt is None:
                continue
            attempts.append(attempt)
            if not attempt.moves_on:
                break
        if not attempts:
            evidence = self.hosted_evidence()
            # The config grows with use: read it off the controller loop.
            refused = await asyncio.to_thread(
                lambda: refused_executors(self.store.load(), tool, evidence=evidence, spec_for=hosted_tool_spec)
            )
            if refused:
                # Sources that run the tool exist and refused it: clearing that refusal is the fix, not another key.
                names = say("modelHub.hostedTools.listSeparator").join(source.display_name for source in refused)
                text = say(f"modelHub.hostedTools.noExecutor.{tool}.refused", sources=names)
                return {"record": failed_record(Failure(text, None))}
            vendors = [catalog_api_key_vendor_label(vendor) or vendor for vendor in executor_vendors(tool)]
            form = "one" if len(vendors) == 1 else "several"
            listed = say("modelHub.hostedTools.listSeparator").join(vendors)
            # Which providers to add is this call's own fact: worded in the user's language and shown as written.
            no_source = Failure(say(f"modelHub.hostedTools.noExecutor.{tool}.{form}", vendors=listed), None)
            return {"record": failed_record(no_source)}
        # The record is the last attempt's; earlier billable ones are listed, each with its own billing.
        last = attempts[-1]
        record = last.record
        if last.moves_on:
            # Every candidate was tried and failed: say which, so the user sees whether retrying or those Sources is the issue.
            tried = ", ".join(dict.fromkeys(attempt.source.display_name for attempt in attempts))
            # Its reason keeps its code; the Sources tried follow in its text.
            last_failure = Failure(record["error"], record["error_code"])
            record = failed_record(
                last_failure.also(say("modelHub.hostedTools.triedSources", sources=tried)), record.get("sources"),
            )
        execution = last.execution()
        failed_attempts = [attempt.failed_attempt() for attempt in attempts[:-1] if attempt.billable]
        if failed_attempts:
            execution["failed_attempts"] = failed_attempts
        return {"record": record, "execution": execution}

    async def run_image_tool(self, payload: object, *, turn: TurnHostedTools | None, language: str = "en") -> dict:
        """Run one ``image_generate`` call directly on the images endpoint of the model it names (§7.4).

        ``turn`` is what the caller's live turn froze: a model outside its enum
        is refused, unrun. There is no chain and no fallback: a failed call says
        which other models the turn offers, and the model may call again with
        one. The call is metered on its Source and never changes Source health.
        """

        def say(key: str, **params: str) -> str:
            return i18n_t(key, language, **params)

        try:
            # Reference images are megabytes of base64: validated off the controller loop.
            call = await asyncio.to_thread(parse_image_call, payload)
        except ExecutorCallRefused as refused:
            raise ModelHubError(refused.code, status=refused.status) from None
        choice = next((item for item in (turn.images if turn is not None else ()) if item.model_id == call.model), None)
        if turn is None or IMAGE_TOOL not in turn.tools or choice is None:
            raise ModelHubError("tool_not_enabled", status=409)
        others = [item.model_id for item in turn.images if item.model_id != call.model]

        def failed(reason: Failure) -> dict:
            # The other models follow the reason in its text; its code stays its own.
            if others:
                reason = reason.also(say("modelHub.hostedTools.imageAlternatives", models=", ".join(others)))
            return {"record": failed_record(reason)}

        unsupported = unsupported_argument(call, choice.spec)
        if unsupported is not None:
            takers = [item.model_id for item in turn.images if unsupported_argument(call, item.spec) is None]
            text = say("modelHub.hostedTools.imageParamUnsupported", model=call.model, param=unsupported)
            if takers:
                text = f"{text} {say('modelHub.hostedTools.imageParamTakers', models=', '.join(takers))}"
            return {"record": failed_record(Failure(text, "image_param_unsupported"))}

        async def admitted() -> ModelHubSourceConfig | None:
            # The config grows with use: read it off the controller loop.
            config = await asyncio.to_thread(self.store.load)
            return admitted_image(config, choice, evidence=self.hosted_evidence())

        unavailable = Failure(say("modelHub.hostedTools.imageModelUnavailable", model=call.model), "image_model_unavailable")
        source = await admitted()
        if source is None:
            return failed(unavailable)
        # Its reference images become data URLs: megabytes, built off the controller loop.
        request = ModelHubRequest(await asyncio.to_thread(image_request, call, choice.spec), protocol="openai_images")
        body = bytearray()

        async def feed(chunk: bytes) -> None:
            if len(body) + len(chunk) > IMAGE_RESPONSE_MAX_BYTES:
                raise ExecutorResponseRefused("modelHub.hostedTools.tooLarge")
            body.extend(chunk)

        called = await self._hosted_upstream_call(
            source, call.model, request, stream=False,
            deadline=asyncio.get_running_loop().time() + IMAGE_TIMEOUT_SECONDS, feed=feed,
            admitted_now=admitted,
        )
        if called is None:
            return failed(unavailable)
        source = called.source
        outcome = called.outcome
        failed_reason = _executor_failure(outcome, say, refused=called.refused)
        # The bytearray itself goes to the worker: no copy of the answer on the loop.
        # Every images endpoint offered makes one image when the call names no n.
        result = await asyncio.to_thread(image_result, body, say, asked=call.n or 1) if failed_reason is None else None
        execution: dict[str, Any] = {
            "mode": "direct",
            "executor_origin": {"provider": source.vendor, "api": source.protocol, "model": call.model},
            "executor_source_id": source.id,
            "executor_billing": source.billing,
        }
        cost = self._image_call_cost(
            source, choice.spec, usage=called.usage, images=len(result.images) if result is not None else 0,
            generation=result.generation if result is not None else None,
        )
        if cost is not None:
            execution["cost"] = _cost_payload(cost)
        if result is None or result.record["status"] != "completed":
            if outcome is not None and outcome.kind is not RawOutcomeKind.SUCCESS:
                await self._learn_image_refusal(source, call.model, outcome)
            if failed_reason is None and result is not None:
                failed_reason = Failure(result.record["error"], result.record.get("error_code"))
            answer = failed(failed_reason or failure(say, "modelHub.hostedTools.unreadable"))
            return {**answer, "execution": execution}
        await self.record_hosted_success(source, call.model, IMAGE_TOOL, "direct")
        answer: dict[str, Any] = {"record": result.record, "execution": execution, "images": [
            {"data": data} for data in result.images
        ]}
        if result.generation is not None:
            answer["generation"] = dict(result.generation)
        return answer

    async def _learn_image_refusal(self, source: ModelHubSourceConfig, model_id: str, outcome: RawCallOutcome) -> None:
        """The image narrow rule (§3.2): an upstream refusal of the model or of the endpoint, before any output.

        The engine's own refusals reach here as local failures, never as an
        upstream status, so they never teach. A refusal of the endpoint itself
        (a 404: the path is not served) holds for every image model the Source
        registers there, so sibling models do not each fail once.
        """

        if outcome.kind is not RawOutcomeKind.HTTP_ERROR or outcome.stream_started:
            return
        detail = f"{outcome.http_status} {outcome.error_code or outcome.error_type or 'error'}"[:64]
        feature = EvidenceFeature.hosted(IMAGE_TOOL, "call")
        if outcome.http_status in {400, 404} and machine_codes_name_model(outcome):
            refused = [model_id]
        elif outcome.http_status == 404:
            refused = [spec.id for spec in image_models_of(source) if source.kind in spec.kinds] or [model_id]
        else:
            return
        await self.record_hosted_refusals(source, [(refused_id, feature, detail) for refused_id in refused])

    def _image_call_cost(
        self,
        source: ModelHubSourceConfig,
        spec: ImageModelSpec,
        *,
        usage: Optional[ProtocolUsageReport],
        images: int,
        generation: Mapping[str, str] | None,
    ) -> Optional[HostedCost]:
        """One image call's cost: included, the list price per delivered image, else its tokens; None when unknown."""

        if source.billing == "monthly":
            return HostedCost(basis="included", usd=None)
        size = (generation or {}).get("size")
        quality = (generation or {}).get("quality")
        matching = [
            entry for entry in spec.prices
            if entry.size in {None, size} and entry.quality in {None, quality}
        ]
        if matching and images:
            # The most specific price that applies to what was delivered.
            price = max(matching, key=lambda entry: (entry.size is not None) + (entry.quality is not None)).price
            return HostedCost(basis="list", usd=round(price.usd_per_1k / 1000 * images, 6))
        return self.hosted_call_cost(source, spec.id, (), usage=usage, counts={})

    async def _executor_attempt(
        self,
        call: ExecutorCall,
        slot: ExecutorSlot,
        *,
        deadline: float,
        say: Callable[..., str],
        admission: Callable[[], Awaitable[ModelHubSourceConfig | None]] | None = None,
    ) -> _ExecutorAttempt | None:
        """Call one executor candidate; None when this snapshot does not admit it, so nothing ran."""

        tool, model_id = call.tool, slot.model_id
        candidate = await asyncio.to_thread(self._admitted_executor, slot, tool)
        if candidate is None:
            return None
        source = candidate.source
        request = executor_request(call, source, model_id, candidate.declaration, candidate.include)
        stream = executor_streams(source)
        reader = ResponsesStreamReader() if stream else None
        body = bytearray()

        async def feed(chunk: bytes) -> None:
            if reader is not None:
                await asyncio.to_thread(reader.feed, chunk)
            elif len(body) + len(chunk) > EXECUTOR_RESPONSE_MAX_BYTES:
                raise ExecutorResponseRefused("modelHub.hostedTools.tooLarge")
            else:
                body.extend(chunk)

        async def admitted_now() -> ModelHubSourceConfig | None:
            if admission is not None:
                return await admission()
            final = await asyncio.to_thread(self._admitted_executor, slot, tool)
            return final.source if final is not None else None

        called = await self._hosted_upstream_call(
            source, model_id, request, stream=stream, deadline=deadline, feed=feed, admitted_now=admitted_now,
        )
        if called is None:
            return None
        source = called.source
        outcome = called.outcome
        failed = _executor_failure(outcome, say, refused=called.refused)
        if reader is not None:
            result = await asyncio.to_thread(responses_result, tool, reader.readout, say, failed=failed)
        elif failed is not None:
            result = None
        else:
            # The bytearray itself goes to the worker: no copy of up to the response budget on the loop.
            result = await asyncio.to_thread(anthropic_result, body, say)
        # The call's cost is the pricing module's, from what the meter saw and what the provider reported.
        counts, reported_usd = provider_charges(source.protocol, result.usage if result is not None else None)
        cost = self.hosted_call_cost(
            source, model_id, (tool,), usage=called.usage, counts=counts, reported_usd=reported_usd,
        )
        if outcome is not None and tool in outcome.hosted_tools and result is not None and result.record["status"] == "completed":
            # A proxy call is a native call on the executor Source: the same positive evidence (§3.2).
            await self.record_hosted_success(source, model_id, tool, "executor")
        # A response that broke a bound reached nothing of Vibey's, so it is a failure before output here (§6
        # step 4): the next candidate is tried. A timeout spent the chain's one deadline.
        moves_on = called.refused is not None
        if (
            outcome is not None and outcome.kind is not RawOutcomeKind.SUCCESS
            and called.refused is None and not called.timed_out
        ):
            moves_on = await self._executor_moves_on(source, model_id, tool, request, outcome)
        return _ExecutorAttempt(
            source=source,
            model_id=model_id,
            record=(
                result.record if result is not None
                else failed_record(failed or failure(say, "modelHub.hostedTools.unreadable"))
            ),
            cost=cost,
            billable=called.billable,
            moves_on=moves_on,
        )

    async def _hosted_upstream_call(
        self,
        source: ModelHubSourceConfig,
        model_id: str,
        request: ModelHubRequest,
        *,
        stream: bool,
        deadline: float,
        feed: Callable[[bytes], Awaitable[None]],
        admitted_now: Callable[[], Awaitable[ModelHubSourceConfig | None]],
    ) -> _HostedUpstreamCall | None:
        """One upstream call a hosted tool makes on its own behalf; None when it was never admitted.

        ``admitted_now`` admits the call again on the snapshot it is made on,
        under the mutation lock. ``feed`` takes the response as it arrives and
        raises ``ExecutorResponseRefused`` when it breaks a bound. The call is
        metered on its Source however it ends, and it never settles Source
        health: it is a tool call, not the turn's model hop.
        """

        handle = None
        cancelled_observed: ProtocolSSEState | None = None
        outcome: RawCallOutcome | None = None
        admitted = False
        refused: str | None = None

        async def invoke() -> None:
            nonlocal handle, outcome, admitted, refused, cancelled_observed, source
            while True:
                await self._prepare_engine_for_demand()
                await self._mutation_lock.acquire()
                held = True

                def release_owner() -> None:
                    nonlocal held
                    if held:
                        held = False
                        self._mutation_lock.release()

                def on_admitted() -> None:
                    nonlocal admitted
                    admitted = True
                    release_owner()

                try:
                    # Admitted again on the snapshot the call is made on, under the lock. The call and
                    # everything it reports (billing, cost, the Source's name) follow that snapshot.
                    final = await admitted_now()
                    if final is None:
                        return
                    source = final
                    if not self._engine_synced:
                        continue
                    try:
                        handle = await self._engine_call(self.adapter.invoke(
                            source.id, model_id, request, stream, "vibey", on_admitted=on_admitted,
                        ))
                    except InvokeCancelledError as cancelled_early:
                        # Cancelled after admission, before a handle: the wire
                        # facts already observed are still billed, so still metered.
                        cancelled_observed = cancelled_early.observed
                        # Raised as a plain cancellation: the deadline converts
                        # only that into its timeout, and any other canceller
                        # reads it as its own.
                        raise asyncio.CancelledError() from None
                    break
                finally:
                    release_owner()
            if handle.stream is not None:
                try:
                    async for chunk in handle.stream:
                        await feed(chunk)
                except ExecutorResponseRefused as broken:
                    # The response broke a bound: the call failed, and nothing more
                    # of it is read. Its usage is still metered, from the outcome
                    # when the call already has one (a buffered response does).
                    refused = broken.key
                    await self._engine_call(handle.close_stream())
                    if handle.outcome_available:
                        outcome = await self._engine_call(handle.outcome())
                    return
            outcome = await self._engine_call(handle.outcome())

        async def settle() -> None:
            # The call ended now; a slow close must not move its usage to a later slice.
            ended_at = self.now()
            try:
                if handle is not None:
                    # Bounded like the gateway's own teardown: a stuck close must
                    # not hold the call past its deadline or the gateway's shutdown.
                    await asyncio.wait_for(handle.close_stream(), timeout=ENGINE_TRANSPORT_TIMEOUT_SECONDS)
            except (asyncio.TimeoutError, Exception):
                logger.warning("Model Hub hosted-tool call stream did not close cleanly", exc_info=True)
            finally:
                if handle is not None or cancelled_observed is not None:
                    await self._meter_call(
                        source_id=source.id,
                        model_id=model_id,
                        outcome=outcome,
                        observed=handle.observed if handle is not None else cancelled_observed,
                        at=ended_at,
                    )

        timed_out = False
        try:
            await asyncio.wait_for(invoke(), timeout=max(0.0, deadline - asyncio.get_running_loop().time()))
        except asyncio.TimeoutError:
            timed_out = True
            if admitted:
                outcome = RawCallOutcome(
                    kind=RawOutcomeKind.TIMEOUT, http_status=None, error_code=None, redacted_message=None,
                    stream_started=False, model_id=model_id, source_id=source.id,
                )
        except ModelHubError as error:
            if not admitted:
                logger.info("Model Hub hosted-tool call was not reached: %s", error.code)
                return None
            outcome = RawCallOutcome(
                kind=RawOutcomeKind.NETWORK_ERROR, http_status=None, error_code=error.code, redacted_message=None,
                stream_started=False, model_id=model_id, source_id=source.id,
            )
        finally:
            # A known outcome belongs to the call even if its caller leaves:
            # drain the bounded settlement before propagating cancellation.
            settlement = asyncio.create_task(settle())
            try:
                await asyncio.shield(settlement)
            except asyncio.CancelledError as cancelled:
                try:
                    await await_owned_task(settlement)
                except BaseException:
                    pass
                raise cancelled
        if not admitted:
            return None
        usage = _call_usage(outcome, handle.observed if handle is not None else cancelled_observed)
        return _HostedUpstreamCall(
            source=source,
            outcome=outcome,
            refused=refused,
            timed_out=timed_out,
            usage=usage,
            # A call that produced nothing and reported no tokens was not billed.
            billable=usage is not None or (
                outcome is not None and (outcome.stream_started or outcome.kind is RawOutcomeKind.SUCCESS)
            ),
        )

    async def _executor_moves_on(
        self,
        source: ModelHubSourceConfig,
        model_id: str,
        tool: str,
        request: ModelHubRequest,
        outcome: RawCallOutcome,
    ) -> bool:
        """Whether a failed executor call passes the search to the next candidate (§6 step 4).

        Nothing reaches Vibey until the chain returns, so a failure mid-response
        counts as before output here. A narrow rule's refusal records its
        evidence, as on any hop. A refusal of the executor model (the Source
        does not serve it) skips the Source for this call only: it names the
        model, not the tool, so it is no evidence about the tool (§6 step 4).
        """

        learned = await self._learn_capability_evidence(source, model_id, request, outcome)
        if learned is not None and learned.feature.kind == "hosted_tool":
            return True
        if not outcome.stream_started and names_model_refusal(outcome):
            if any(model.id == model_id and model.provenance == "discovered" for model in source.models):
                # An executor runs only on a model its Source lists, so the
                # listing may be stale: mark it for the schedule (#2682). A
                # manual row is the user's, and only they remove it.
                self.mark_stale_listing(source, model_id)
            return True
        decision = classify_outcome(replace(outcome, stream_started=False))
        return decision.action in {"fallback", "refresh"}

    def freeze_turn_hosted_tools(self, config: ModelHubConfig) -> TurnHostedTools:
        """What a Vibey turn's first resolution freezes: its tools, executor orders and image models (§3.4)."""

        return freeze_turn_tools(config, evidence=self.hosted_evidence(), spec_for=hosted_tool_spec)

    def hosted_tools_for_hop(
        self, source: ModelHubSourceConfig, model_id: str, turn: TurnHostedTools,
    ) -> dict:
        """``HopResolution.hosted_tools`` for a hop of a Vibey turn served by this Source and model."""

        return hop_hosted_tools(turn, source, model_id, evidence=self.hosted_evidence(), spec_for=hosted_tool_spec)

    async def set_agent_mode(self, backend: str, mode: object) -> dict:
        if mode not in {"hub", "direct"} or (backend == "vibey" and mode != "hub"):
            raise ModelHubError("mode_switch_blocked")

        # A mode is a launch input like any other: running work finishes in the
        # mode it started in, and each runtime moves at its next turn, so the
        # switch interrupts nothing. The gateway keeps serving a Hub turn that
        # was admitted before a switch to Direct. Mode-only consent never
        # imports a login: native auth stays native beside the Hub, where the
        # migration dialog can still take it over later.
        name = cast(BackendName, backend)
        async with self._migration_lock:
            async with self._mutation_lock:
                previous = self.store.load()
                agent = self._agent(previous, backend)
                if agent.mode == mode:
                    # A committed switch is applied only once the runtime has
                    # adopted it; a retry of the same mode finishes that.
                    if name not in self._pending_builtin_catalog_refresh:
                        return self._agent_payload(previous, agent)
                    committed = previous
                else:
                    try:
                        if mode == "hub" and await self._native_config_blocks_launch(previous, backend):
                            raise ModelHubError("mode_switch_blocked", status=409)
                        config = self._clone_config(previous)
                        self._agent(config, backend).mode = mode
                        if mode == "hub":
                            self._carry_agent_selections(config, name)
                        await self._commit_synced(previous, config)
                    except (TakeoverStateError, OSError):
                        raise ModelHubError("mode_switch_blocked", status=409) from None
                    committed = self.store.load()
                    self._pending_builtin_catalog_refresh.add(name)
            await self._refresh_backend_catalog(name)
            self._pending_builtin_catalog_refresh.discard(name)
            return self._agent_payload(committed, self._agent(committed, backend))

    async def reorder_agent_chains(
        self,
        backend: str,
        order: object = _REORDER_ORDER_UNSET,
        *,
        force: bool = False,
        confirmed_remove_hops: object = None,
        confirmed_interruptions: object = None,
    ) -> dict:
        if order is _REORDER_ORDER_UNSET:
            return self.get_agent_sources(backend)
        return await self.set_agent_sources(backend, {
            "order": order,
            "force": force,
            "would_remove_hops": confirmed_remove_hops,
            "would_interrupt": confirmed_interruptions,
        })

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
        rows: list[ModelHubBackendModelConfig] = []
        for item in payload:
            if backend == "claude" and isinstance(item, dict) and item.get("id") == "default":
                raise ModelHubError("backend_model_id_invalid")
            try:
                model = ModelHubBackendModelConfig.from_payload(item)
            except (TypeError, ValueError) as exc:
                raise ModelHubError("backend_model_catalog_invalid") from exc
            if backend == "opencode" and model.native_protocol is None:
                raise ModelHubError("backend_model_catalog_invalid")
            if backend != "opencode" and model.native_protocol is not None:
                raise ModelHubError("backend_model_catalog_invalid")
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
            touched = False
            # Earlier releases stored a Claude row's native ` [1M]` suffix as its
            # name (see `builtin_display_name`); give those rows back their id.
            if backend in snapshots:
                for model in agent.models:
                    if (
                        model.origin == "builtin"
                        and model.display_name is not None
                        and builtin_display_name(backend, model.id, model.display_name) is None
                    ):
                        model.display_name = None
                        touched = True
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
                touched = True
            if touched:
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

    async def reconcile_source_reasoning_tiers(self) -> list[tuple[str, str]]:
        """Derive saved Source tiers again by the current ladder, once at startup; the (Source, model) pairs it changed.

        Rows an earlier build derived keep its rules until discovery revisits
        them: 3.2.0 found no catalog row for a snapshot id and declared the whole
        protocol default list for Opus 4.6. ``rederive_persisted_tiers`` decides
        which rows are derived again, and an agent's provider rows that still hold
        what those rows gave follow them. The result is saved once, so a later
        start changes nothing.
        """

        async with self._mutation_lock:
            previous = self.store.load()
            config = self._clone_config(previous)
            catalog_efforts_by_model = bundled_catalog_wire_efforts_by_model()
            changed: list[tuple[str, str]] = []
            for source in config.sources:
                for model in source.models:
                    resolution = rederive_persisted_tiers(
                        protocol=source.protocol,
                        model_id=model.id,
                        efforts=model.reasoning_efforts,
                        source=model.reasoning_efforts_source,
                        catalog_efforts_by_model=catalog_efforts_by_model,
                    )
                    if resolution is None or (
                        list(resolution.efforts) == list(model.reasoning_efforts)
                        and resolution.source == model.reasoning_efforts_source
                    ):
                        continue
                    model.reasoning_efforts = list(resolution.efforts)
                    model.reasoning_efforts_source = resolution.source
                    changed.append((source.id, model.id))
            if changed:
                # A provider row in an agent's list took its tiers from its suppliers' Source rows when it was added,
                # and a hop offers only the tiers its row lists (Vibey sends no other). A row that still holds what
                # its suppliers gave follows them; one that holds anything else was set apart and stays.
                for backend, agent in config.agents.items():
                    for row in agent.models:
                        if row.origin != "provider":
                            continue
                        before = self._candidate_suppliers(previous, backend, row.id)[2]
                        after = self._candidate_suppliers(config, backend, row.id)[2]
                        if after != before and list(row.reasoning_efforts) == before:
                            row.reasoning_efforts = after
            if changed and self._reconcile_store_writable():
                await self._commit_synced(previous, config)
                return changed
            return []

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
            # Two different refusals, and they are separate codes because they
            # name different next steps. Forging `builtin` is a claim about a
            # model this backend publishes, and the way out is to drop the row.
            # Rewriting a saved row's origin is a claim about a row that already
            # exists, and the way out is to EDIT that row instead of removing it
            # and adding it again — advice the built-in wording cannot carry, and
            # which the merged code left unsayable on a backend like OpenCode
            # whose built-in snapshot is empty by construction.
            for model_id, desired in desired_by_id.items():
                trusted = current_by_id.get(model_id) or baseline_by_id.get(model_id)
                if trusted is None:
                    if desired.origin == "builtin" and model_id not in builtin_ids:
                        raise ModelHubError("backend_model_locked", status=409)
                elif desired.origin != trusted.origin:
                    # `origin` records how a row was FIRST created, so a saved
                    # row keeps its own answer however often it is re-filled.
                    raise ModelHubError(
                        "backend_model_origin_immutable",
                        status=409,
                    )
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
                route = effective_model_route(config, agent_backend, model_id)
                current = current_by_id.get(model_id)
                if current is not None and current != baseline_by_id[model_id]:
                    raise ModelHubError("backend_model_conflict", status=409)
                if route.hops:
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
                merged_by_id.pop(model_id, None)
                agent.routes.pop(model_id, None)
                if model_id not in agent.removed_model_ids:
                    agent.removed_model_ids.append(model_id)

            agent.models = [
                model
                for model in agent.models
                if model.id not in removed_model_id_set
            ]

            for model_id, desired in desired_by_id.items():
                baseline_model = baseline_by_id.get(model_id)
                current = current_by_id.get(model_id)
                if baseline_model is None:
                    if current is not None and current != desired:
                        raise ModelHubError("backend_model_conflict", status=409)
                    merged_by_id[model_id] = desired
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
            if agent.backend == "opencode":
                agent.menu = ModelHubMenuConfig(
                    view=agent.menu.view if agent.menu else "featured",
                    checked=list(ordered_ids),
                )
            # The guard plans against the complete staged catalog, including the
            # rows this same save adds, edits and reorders: a removed row's Agent
            # may run on one of them afterwards. A save that conflicts has
            # already refused above, so it never asks a confirmation for nothing.
            interrupted = self._removed_agent_models(
                previous,
                config,
                agent_backend,
                removed_model_id_set,
            )
            self._require_guard_plan(
                force=force,
                confirmed_remove_hops=confirmed_remove_hops,
                confirmed_interruptions=confirmed_interruptions,
                would_remove_hops=removed_hops,
                would_interrupt=interrupted,
                error="backend_model_in_route",
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
            if removed_hops or interrupted:
                result["removed_hops"] = removed_hops
                result["interrupted"] = interrupted
            return result

    @staticmethod
    def models_dev_matches(query: object) -> list[dict]:
        if not isinstance(query, str) or not query.strip():
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
            if source.protocol == "search":
                raise ModelHubError("mapping_target_unavailable")
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
            retired_hops = [
                item for item in self._effective_hop_refs(previous)
                if model.provenance == "discovered" and item["source_id"] == source.id and item["model_id"] == model_id
            ]
            if model.provenance == "discovered":
                model.retired = True
            else:
                source.models = [
                    item
                    for item in source.models
                    if item.id != model_id
                ]
            invalidated = (
                self._invalidated_route_hops(config, source.id)
                if not source_supports_passthrough(source)
                else []
            )
            self._prune_invalidated_route_hops(config, [*retired_hops, *invalidated])
            removed_hops = self._removed_effective_hops(previous, config)
            removed_hops.extend(item for item in retired_hops if item not in removed_hops)
            removed_hops.extend(item for item in invalidated if item not in removed_hops)
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
            await self._commit_synced(previous, config)
            return {
                "source": self._source_payload(source),
                "removed_hops": removed_hops,
                "interrupted": would_interrupt,
            }

    def usage_summary(
        self,
        *,
        days: Optional[int] = None,
        window: Optional[str] = None,
    ) -> dict:
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

        if window is not None and days is not None:
            raise ModelHubError("invalid_parameter", status=400)
        if window is not None and window not in USAGE_WINDOW_KEYS:
            raise ModelHubError("invalid_parameter", status=400)

        config = self.store.load()
        identities = [
            SourceIdentity(
                source_id=source.id,
                label=source.display_name,
                model_ids=[model.id for model in source.models],
            )
            for source in config.sources
        ]
        # The release's search services label and value service rows, as the price
        # table values model tokens; a row a later catalog dropped stays unpriced.
        service_catalog = [
            ServiceCatalogEntry(
                service_id=entry.search["service_id"],
                label=entry.label,
                usd_per_1k=float(entry.search["price"]["usd_per_1k"]),
                price_date=entry.search["price"]["as_of"],
            )
            for entry in api_key_vendor_catalog()
            if entry.search is not None
        ]
        now = self.now()
        prices = self.price_table()
        if window is not None:
            return self.usage.report(
                window=window, now=now, identities=identities, prices=prices, service_catalog=service_catalog
            )
        return self.usage.summary(
            days=USAGE_DEFAULT_WINDOW_DAYS if days is None else days,
            now=now,
            identities=identities,
            prices=prices,
            service_catalog=service_catalog,
        )

    async def _fetch_subscription_quota(self, source_id: str, vendor: str, credential_ref: str) -> Mapping[str, Any]:
        reader = getattr(self.adapter, "subscription_quota", None)
        if not callable(reader):
            raise SubscriptionQuotaError("unavailable")
        return await reader(source_id, vendor, credential_ref)

    def _quota_sources(self) -> list[QuotaSourceRef]:
        config = self.store.load()
        refs = []
        for source in config.sources:
            if source.kind != "subscription" or source.supply_channel != "hub" or not source.credential_ref:
                continue
            payload = self._source_account_payload(
                {
                    "id": source.id,
                    "kind": source.kind,
                    "vendor": source.vendor,
                    "supply_channel": source.supply_channel,
                    "credential_ref": source.credential_ref,
                    "account_label": source.account_label,
                }
            )
            refs.append(
                QuotaSourceRef(
                    source_id=source.id,
                    vendor=source.vendor.strip().lower(),
                    credential_ref=source.credential_ref,
                    display_name=source.display_name,
                    account_label=payload.get("account_label"),
                )
            )
        return refs

    async def quota_summary(self, *, force: bool = False) -> dict:
        """Report each hub-held subscription's rate-limit windows.

        A report only, like usage metering: nothing in resolution reads it. The
        cache re-reads a Source at most every five minutes, or every thirty
        seconds when forced, and keeps the last good snapshot across failures.
        """

        sources = await asyncio.to_thread(self._quota_sources)
        summary = await self.quota.summary(sources, force=force)
        try:
            summary["value"] = await asyncio.to_thread(self._quota_values, summary["sources"])
        except Exception:  # noqa: BLE001 - the valuation is optional; the windows are not
            logger.warning("Model Hub quota valuation failed", exc_info=True)
            for source in summary["sources"]:
                source.pop("value", None)
        return summary

    def _quota_values(self, sources: list[dict[str, Any]]) -> dict[str, Any]:
        config = self.store.load()
        wanted = {source["source_id"] for source in sources}
        identities = [
            SourceIdentity(
                source_id=source.id,
                label=source.display_name,
                model_ids=[model.id for model in source.models],
            )
            for source in config.sources
            if source.id in wanted
        ]
        prices = self.price_table()
        now = self.now()
        return quota_values(
            sources,
            daily_costs=self.usage.daily_costs(
                days=VALUE_WINDOW_DAYS,
                now=now,
                identities=identities,
                prices=prices,
            ),
            prices=prices,
            today=local_usage_day(now),
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
        live_recovery: Mapping[str, SourceRecoveryAnnotation] | None = None,
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
            live_recovery=live_recovery if live_recovery is not None else self.recovery_annotations(config),
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
                "backoff" if inspection.backoff
                else "cooldown" if inspection.cooldown
                else "healthy"
                if status in {"active", "standby", "cooldown"}
                else status
            )
            refused = self._refused_efforts(source, inspection.model_id, observed_at)
            chain.append(
                {
                    "source_id": source.id,
                    "model_id": inspection.model_id,
                    "channel": source.supply_channel,
                    "health": health,
                    "runnable": inspection.runnable,
                    "reason": inspection.reason,
                    "retry_at": inspection.retry_at,
                    **({"recovery": inspection.recovery} if inspection.recovery is not None else {}),
                    **({"refused_efforts": refused} if refused else {}),
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
            "manual_override": resolution.manual_override.to_payload() if resolution.manual_override is not None else None,
            "route_origin": resolution.route_origin,
            "chain": chain,
            "current": current,
            "supply_state": (
                "ok" if resolution.candidate_hops
                else "waiting" if resolution.supply_status == "waiting"
                else "interrupted"
            ),
        }

    def _refused_efforts(self, source: ModelHubSourceConfig, model_id: str, now: datetime) -> list[dict]:
        """The efforts this hop currently refuses, as routing reads the evidence."""

        evidence = self.hosted_evidence()
        refused = [
            record
            for record in self.capability_evidence.for_hop(source.id, model_id)
            if record.feature.kind == "effort" and evidence.refusal(source, model_id, record.feature) is record
        ]
        return [
            {
                "effort": record.feature.name,
                "basis": "observed",
                "observed_at": record.payload()["observed_at"],
                "detail": record.detail,
            }
            for record in sorted(refused, key=lambda item: (item.observed_at, item.feature.name))
        ]

    def agent_chain(self, backend: str, model_id: object) -> dict:
        if backend not in MODEL_HUB_BACKENDS or not isinstance(model_id, str) or not model_id:
            raise ModelHubError("mapping_target_unavailable", status=409)
        config = self.store.load()
        return self._agent_chain(config, backend, model_id, live_recovery=self.recovery.annotations(config))

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
        live_recovery = self.recovery.annotations(config)
        hidden = self._hidden_retired_model_ids(agent) - {requested_model}
        return [
            self._agent_chain(
                config,
                backend,
                model_id,
                now=observed_at,
                unavailable_source_ids=unavailable_source_ids,
                live_recovery=live_recovery,
            )
            # The overview is a picker surface; routing keeps every persisted row.
            for model_id in self._agent_model_ids(agent, requested_model)
            if model_id not in hidden
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
        *,
        output_tokens: int = 1,
    ) -> ModelHubRequest:
        # A probe enters the same translation seam as a live backend turn, so
        # its payload must be shaped in the backend's client protocol.
        request_protocol = _FIXED_BACKEND_PROTOCOLS.get(backend, source.protocol)
        if request_protocol == "anthropic":
            payload = {
                "model": model_id,
                "max_tokens": output_tokens,
                "messages": [{"role": "user", "content": "ping"}],
            }
        elif request_protocol == "openai_responses":
            payload = {
                "model": model_id,
                "max_output_tokens": output_tokens,
                "input": "ping",
            }
        elif request_protocol == "google":
            payload = {
                "contents": [{"role": "user", "parts": [{"text": "ping"}]}],
                "generationConfig": {"maxOutputTokens": output_tokens},
            }
        else:
            payload = {
                "model": model_id,
                "max_tokens": output_tokens,
                "messages": [{"role": "user", "content": "ping"}],
            }
        return ModelHubRequest(payload, protocol=request_protocol)

    async def probe_source(self, source_id: str, payload: object) -> dict:
        """Test one saved API-key model, independently of Agent route/health."""
        if not isinstance(payload, dict) or set(payload) != {"model"}:
            raise ModelHubError("discovery_failed")
        model_id = payload["model"]
        # This selects an existing inventory identity, not a newly admitted ID.
        # Legacy persisted IDs retain their load-time spelling.
        if not isinstance(model_id, str) or not model_id.strip():
            raise ModelHubError("discovery_failed")
        model_id = normalized_model_id(model_id)

        def selected(config: ModelHubConfig) -> ModelHubSourceConfig:
            source = self._source(config, source_id)
            if (
                source.kind != "api_key"
                or source.supply_channel != "hub"
                or source.protocol == "search"
                or not source.credential_ref
                or not any(model.id == model_id and not model.retired for model in source.models)
            ):
                raise ModelHubError("mapping_target_unavailable", status=409)
            # Saved-Source tests are buffered and speak the Source protocol.
            # Check both initial selection and the locked post-prepare snapshot.
            reason = _google_admission_skip_reason(source.protocol, source.protocol, model_id, False)
            if reason is not None:
                raise ModelHubError(reason, status=422)
            return source

        async def drain(handle: InvokeHandle) -> None:
            if handle.stream is not None:
                async for _chunk in handle.stream:
                    pass

        source, outcome, elapsed_ms = await self._user_test_call(
            source_id,
            model_id,
            selected,
            # OpenCode preserves the request's explicit protocol and API keys
            # have unrestricted origins.
            lambda source: self._probe_request(source, model_id, "opencode", output_tokens=128),
            stream=False,
            origin="opencode",
            consume=drain,
            timeout=_SOURCE_PROBE_TIMEOUT_SECONDS,
        )
        succeeded = outcome.kind is RawOutcomeKind.SUCCESS
        return {
            "source_id": source.id,
            "model_id": model_id,
            "protocol": source.protocol,
            "reachable": succeeded,
            "latency_ms": elapsed_ms,
            "error": None if succeeded else self._test_call_failure(outcome),
        }

    def _test_call_failure(self, outcome: RawCallOutcome) -> str:
        """A failed user test call's detail key, for display only: it blocks nothing and changes no route state."""

        decision = classify_outcome(outcome)
        if decision.action == "refresh":
            decision = ResolutionDecision("fallback", reason="credential_revoked")
        return self._probe_failure(outcome, decision, recovery_managed=False)[0]

    async def _user_test_call(
        self,
        source_id: str,
        model_id: str,
        selected: Callable[[ModelHubConfig], ModelHubSourceConfig],
        request: Callable[[ModelHubSourceConfig], ModelHubRequest],
        *,
        stream: bool,
        origin: str,
        consume: Callable[[InvokeHandle], Awaitable[None]],
        timeout: float,
    ) -> UserTestCall:
        """One call a user asked for, on exactly this Source and model: no Agent route, no failover.

        ``selected`` re-checks the Source on every snapshot it is invoked from,
        and ``consume`` reads the opened call. The call is settled whatever
        happens: a success counts as the retry a cooldown waits for and verifies
        the credential, a failure changes no route state, and the call is
        metered. Returns the Source as invoked, its outcome, its latency, and
        whether this call retired the Source's pending verification itself.
        """

        handle = None
        outcome = None
        source = None
        admitted_at = None
        attempt_generation = PRE_ATTEMPT_SETTLEMENT_GENERATION
        cancelled_observed: ProtocolSSEState | None = None

        async def invoke_selected() -> None:
            nonlocal source, handle, outcome, admitted_at, attempt_generation, cancelled_observed
            # Bound local waiting too, but never call it a model failure before
            # the adapter has actually admitted this Source/model invocation.
            async with self._mutation_lock:
                selected(await asyncio.to_thread(self.store.load))
            while True:
                await self._prepare_engine_for_demand()
                await self._mutation_lock.acquire()
                held = True

                def release_owner() -> None:
                    nonlocal held
                    if held:
                        held = False
                        self._mutation_lock.release()

                def admitted() -> None:
                    nonlocal admitted_at, attempt_generation
                    admitted_at = time.monotonic()
                    # Ordered at admission but not reserved: a failed test must
                    # not displace another attempt's verdict, while a successful
                    # one supersedes attempts admitted before it.
                    attempt_generation = self._mint_settlement_generation()
                    release_owner()

                try:
                    source = ModelHubSourceConfig.from_payload(
                        selected(await asyncio.to_thread(self.store.load)).to_payload(),
                    )
                    if not self._engine_synced:
                        continue
                    # No Agent resolver is involved: the adapter binds this exact Source.
                    try:
                        handle = await self._engine_call(self.adapter.invoke(
                            source.id,
                            model_id,
                            request(source),
                            stream,
                            origin,
                            on_admitted=admitted,
                        ))
                    except InvokeCancelledError as cancelled_early:
                        # Cancelled after admission, before a handle (a timeout
                        # included): the wire facts already observed are billed,
                        # so they are still metered. Raised on as a plain
                        # cancellation, which the deadline below reads as its own
                        # timeout and any other canceller as theirs.
                        cancelled_observed = cancelled_early.observed
                        raise asyncio.CancelledError() from None
                    break
                finally:
                    release_owner()
            if admitted_at is None:
                raise ModelHubError("engine_down", status=503)
            await consume(handle)
            outcome = await self._engine_call(handle.outcome())

        async def settle_attempt() -> None:
            try:
                if source is not None and outcome is not None:
                    # The explicit test is the retry a cooldown waits for.
                    if self._verified_recovery_outcome(outcome):
                        await self._record_recovery_success(
                            source.id, attempt_generation, backend="system", model_id=model_id,
                        )
                    await self._verify_successful_source(
                        source.id, source.credential_ref, source.verification_pending, outcome,
                    )
            finally:
                try:
                    if handle is not None:
                        await handle.close_stream()
                finally:
                    if handle is not None or cancelled_observed is not None:
                        await self._meter_call(
                            source_id=source_id, model_id=model_id, outcome=outcome,
                            observed=handle.observed if handle is not None else cancelled_observed,
                        )

        try:
            await asyncio.wait_for(invoke_selected(), timeout=timeout)
        except asyncio.TimeoutError:
            if admitted_at is None:
                raise ModelHubError("engine_down", status=503) from None
            outcome = RawCallOutcome(
                kind=RawOutcomeKind.TIMEOUT,
                http_status=None,
                error_code=None,
                redacted_message=None,
                stream_started=False,
                model_id=model_id,
                source_id=source_id,
            )
        finally:
            elapsed_ms = max(0, round((time.monotonic() - admitted_at) * 1000)) if admitted_at is not None else 0
            # A known outcome belongs to the attempt even if its caller leaves.
            # Drain finite settlement before propagating caller cancellation.
            settlement_task = asyncio.create_task(settle_attempt())
            try:
                await asyncio.shield(settlement_task)
            except asyncio.CancelledError as cancelled:
                try:
                    await await_owned_task(settlement_task)
                except BaseException:
                    # Cancellation owns the response, not resource settlement.
                    pass
                raise cancelled
        assert source is not None and outcome is not None
        return UserTestCall(source, outcome, elapsed_ms)

    @staticmethod
    def _probe_failure(
        outcome: RawCallOutcome,
        decision: ResolutionDecision,
        *,
        recovery_managed: bool,
    ) -> tuple[str, Optional[EventReason]]:
        if decision.action == "surface":
            return "models.source.error.unclassified", None
        if outcome.kind == RawOutcomeKind.NETWORK_ERROR:
            return (
                "models.source.backoff.connection_failed" if recovery_managed else "models.source.cooldown.network",
                "network",
            )
        if outcome.kind == RawOutcomeKind.TIMEOUT:
            return (
                "models.source.backoff.connection_failed" if recovery_managed else "models.source.cooldown.timeout",
                "network",
            )
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
        if decision.reason == "plan_excludes_product":
            return "models.source.needs_action.plan_excludes_product", "plan_excludes_product"
        if decision.reason == "spend_limit_reached":
            return "models.source.needs_action.spend_limit_reached", "spend_limit_reached"
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

    async def _write_source_blocker_locked(
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
        persisted = await self._save_runtime_config(previous, config)
        if persisted:
            # A blocked grant must not keep reporting the windows it had while usable.
            self.quota.forget(source.id)
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
        while True:
            try:
                return await self._probe_agent_once(backend, model_id)
            except _InvocationUnsupported as skipped:
                # Explicit probes select one hop, not a fallback turn. A
                # permanent local limitation cannot restart as route churn.
                raise ModelHubError(skipped.reason, status=422) from None
            except _InvocationPlanChanged:
                continue

    async def _probe_agent_once(self, backend: str, model_id: object) -> dict:
        if backend not in MODEL_HUB_BACKENDS:
            raise ModelHubError("mapping_target_unavailable", status=409)
        async with self._mutation_lock:
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
                key=parse_model_hub_timestamp,
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
        verification_pending = source.verification_pending
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

        if backend == "vibey":
            reason = _google_admission_skip_reason(source.protocol, source.protocol, resolved_model, False)
            if reason is not None:
                raise _InvocationUnsupported(reason)
        await self._prepare_engine_for_demand()
        started_at = time.monotonic()
        settlement_generation = None

        def admitted(generation: int) -> None:
            nonlocal settlement_generation
            settlement_generation = generation

        handle: InvokeHandle | None = None
        try:
            handle = await self._invoke_admitted(
                source=source,
                model_id=resolved_model,
                requested_model_id=chain_payload["model_id"],
                request=self._probe_request(source, resolved_model, backend),
                stream=False,
                backend=cast(BackendName, backend),
                excluded_source_ids=set(),
                on_admitted=admitted,
            )
            if handle.stream is not None:
                async for _chunk in handle.stream:
                    pass
            outcome = await self._engine_call(handle.outcome())
            decision = await self._classify_source_outcome(source, outcome)
            if decision.action == "refresh":
                await handle.close_stream()
                self.recovery.release(source.id, settlement_generation)
                handle = await self._invoke_admitted(
                    source=source,
                    model_id=resolved_model,
                    requested_model_id=chain_payload["model_id"],
                    request=self._probe_request(source, resolved_model, backend),
                    stream=False,
                    backend=cast(BackendName, backend),
                    excluded_source_ids=set(),
                    exact_retry=True,
                    on_admitted=admitted,
                )
                if handle.stream is not None:
                    async for _chunk in handle.stream:
                        pass
                outcome = await self._engine_call(handle.outcome())
                decision = classify_outcome(outcome, refresh_attempted=True)
            elapsed_ms = max(0, round((time.monotonic() - started_at) * 1000))
            reachable = decision.action == "return"
            if reachable:
                if self._verified_recovery_outcome(outcome):
                    await self._record_recovery_success(
                        source.id, settlement_generation,
                        backend=cast(BackendName, backend), model_id=chain_payload["model_id"],
                    )
                await self._verify_successful_source(
                    source.id, source.credential_ref, verification_pending, outcome,
                )
            error_key: Optional[str] = None
            latency_ms: Optional[int] = elapsed_ms
            if not reachable:
                error_key, event_reason = self._probe_failure(outcome, decision, recovery_managed=True)
                if error_key == "models.source.backoff.connection_failed":
                    latency_ms = None
                if event_reason is not None:
                    await self._settle_fallback_source(
                        source,
                        decision,
                        backend=cast(BackendName, backend),
                        model_id=chain_payload["model_id"],
                        detail_key=error_key,
                        settlement_generation=settlement_generation,
                        outcome=outcome,
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
        finally:
            try:
                if handle is not None:
                    await await_owned_task(asyncio.create_task(handle.close_stream()))
            finally:
                self.recovery.release(source.id, settlement_generation)

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
                # The indexed expression, verbatim: a seek on this turn's rows
                # instead of a scan of every Message under the write lock.
                .where(literal_column(MESSAGE_TURN_ID_SQL) == normalized)
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
        # The turn's row is found alone, by the indexed expression: joined to
        # ``agent_sessions``, whose ``metadata_json`` shares the column name, the
        # expression's unqualified column would be ambiguous.
        turn_row = (
            select(
                messages.c.session_id,
                func.json_extract(
                    messages.c.metadata_json,
                    "$.model_hub_mode",
                ).label("mode"),
            )
            .where(literal_column(MESSAGE_TURN_ID_SQL) == turn_id)
            .limit(1)
            .subquery()
        )
        try:
            with get_cached_sqlite_engine().connect() as conn:
                row = conn.execute(
                    select(
                        agent_sessions.c.agent_backend.label("backend"),
                        turn_row.c.mode,
                    ).select_from(
                        turn_row.join(
                            agent_sessions,
                            turn_row.c.session_id == agent_sessions.c.id,
                        )
                    )
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

    def get_model_provenance(self, backend: str, model_id: object) -> dict | None:
        if backend not in MODEL_HUB_BACKENDS or not isinstance(model_id, str) or normalized_model_id(model_id) != model_id:
            raise ModelHubError("mapping_target_unavailable", status=409)
        agent = self.store.load().agents[backend]
        if model_id not in self._agent_menu_model_ids(agent):
            raise ModelHubError("mapping_target_unavailable", status=409)
        return self.provenance.latest_for_model(backend, model_id)

    async def reauth_source(self, source_id: str, payload: object) -> dict:
        if (
            not isinstance(payload, dict)
            or set(payload) - {"acknowledge_irreversible"}
            or payload.get("acknowledge_irreversible") is not True
        ):
            raise ModelHubError("reauth_confirmation_required", status=409)

        from core.backend_restart import NativeMigrationBlockedError
        from vibe.native_oauth_store import NativeOAuthError

        try:
            await await_owned_task(asyncio.create_task(
                prepare_takeover_reauthentication(self, source_id),
                name="model-hub-takeover-reauthentication",
            ))
        except NativeMigrationBlockedError:
            raise ModelHubError("migration_native_busy", status=409) from None
        except (MigrationConflictError, TakeoverStateError, NativeOAuthError, OSError):
            raise ModelHubError("migration_recovery_pending", status=409) from None

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
                self._save_projection_neutral_blocking(previous, config)

                def restore_after_spawn_failure() -> None:
                    self._save_projection_neutral_blocking(config, previous)

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
        # ``test_nonce_oauth_start_retry_arriving_at_the_claim_joins_the_owner``
        # retries at the claim so the next pre-check cannot re-open the window.
        async def start_and_remember() -> dict:
            pending_source_id = _source_id()
            flow: OAuthFlowState | None = None
            flow_cleanup_done = False
            flow_cleanup_attempted = False
            try:
                if oauth_channel == "native_cli":
                    await self._settle_pending_native_creates(vendor)
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

    async def runtime_ensure_dependency(
        self,
        *,
        force: bool = False,
        offline: bool = False,
    ) -> dict:
        """Converge the pinned engine without changing persisted run intent."""

        async with self._runtime_lifecycle_lock:
            await self.reconcile_runtime_installation()
            result = await self._ensure_runtime_dependency(
                force=force,
                offline=offline,
            )
            return runtime_dependency_payload(
                result,
                enabled=self.store.load().enabled,
            )

    async def runtime_start(self) -> dict:
        async with self._runtime_lifecycle_lock:
            await self.reconcile_runtime_installation()
            async with self._mutation_lock:
                previous = self.store.load()
                updated = self._clone_config(previous)
                updated.enabled = True
                updated.runtime_default_applied = True
                await self._save_projection_neutral(previous, updated)
                # A start renders the persisted projection, whose text-only
                # marks come from the catalog copy of its last sync. Syncing
                # here gives an explicit start the current copy's marks.
                self._engine_synced = False
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
                    and (backend != "vibey" or agent.models)
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
                updated.runtime_default_applied = True
                await self._save_projection_neutral(previous, updated)
                return _runtime_payload(status, enabled=False)

    def migration_scan(self) -> dict:
        try:
            pending = self.migration_journal.load()
        except (TakeoverStateError, OSError):
            raise ModelHubError("migration_item_conflict", status=409) from None
        if pending is not None:
            # Resume the selected operation even though native credentials
            # have already been withdrawn. Never expose the private journal.
            return {"items": pending["items"]}
        config = self.store.load()
        return {
            "items": [
                item.to_payload()
                for item in scan_native_configs(
                    config,
                    mask_credential=_mask_credential,
                    home=self.migration_home,
                    validate_base_url=_validated_base_url,
                    project_roots=self.migration_project_roots(),
                    clean_native_stores=(self.migration_journal.completed() or {}).get("clean_native_stores"),
                    retained_native_ids=(self.migration_journal.completed() or {}).get("retained_native_ids"),
                    legacy_auth=(
                        self.store.native_auth_snapshot(tuple(sorted(NATIVE_CLI_BACKENDS)))
                        if isinstance(self.store, V2ModelHubConfigStore) else None
                    ),
                )
            ]
        }

    async def migration_apply(self, item_ids: object, clean_api_keys: object = False) -> dict:
        from core.backend_restart import NativeMigrationBlockedError
        from vibe.native_oauth_store import NativeOAuthError, NativeOAuthPermissionError

        try:
            if not isinstance(clean_api_keys, bool):
                raise MigrationConflictError
            selection = (
                (tuple(sorted(item_ids)), clean_api_keys)
                if isinstance(item_ids, list) and all(isinstance(value, str) for value in item_ids)
                else None
            )
            task = self._migration_task
            if task is not None and not task.done():
                if selection != self._migration_item_ids:
                    raise ModelHubError("migration_native_busy", status=409)
            else:
                task = asyncio.create_task(apply_native_migration(
                    self, item_ids, mask_credential=_mask_credential,
                    validate_base_url=_validated_base_url,
                    clean_api_keys=clean_api_keys,
                ), name="model-hub-native-takeover")
                self._migration_task = task
                self._migration_item_ids = selection
            applied, added_to = await await_owned_task(task)
        except MigrationCredentialsInvalidError:
            raise ModelHubError("migration_credentials_invalid", status=409) from None
        except MigrationReauthorizationRequiredError:
            raise ModelHubError("migration_reauthorization_required", status=409) from None
        except NativeMigrationBlockedError as error:
            _log_native_refusal("Model Hub migration", error)
            code = (
                "migration_runtime_stopping"
                if error.reason in _RUNTIME_STOPPING_REASONS
                else "migration_native_busy"
            )
            raise ModelHubError(code, status=409) from None
        except (NativeOAuthPermissionError, PermissionError):
            raise ModelHubError("migration_permission_needed", status=409) from None
        except (TakeoverStateError, NativeOAuthError):
            raise ModelHubError("migration_configuration_blocked", status=409) from None
        except (MigrationConflictError, OSError):
            raise ModelHubError("migration_item_conflict", status=409)
        except ModelHubError as exc:
            try:
                pending = self.migration_journal.load()
            except (TakeoverStateError, OSError):
                self.migration_blocked_backends.update(NATIVE_CLI_BACKENDS)
                raise ModelHubError("migration_configuration_blocked", status=409) from None
            if pending is not None and pending["phase"] == "exposed":
                raise ModelHubError("migration_recovery_pending", status=409) from None
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
        # Compatibility for existing callers: elapsed timers only admit a try.
        self.recovery.annotations(self.store.load())

    async def _record_recovery_success(
        self, source_id: str, generation: int | None, *, backend: EventAgent, model_id: str,
    ) -> None:
        async with self._mutation_lock:
            config = self.store.load()
            source = next((item for item in config.sources if item.id == source_id), None)
            if (
                source is None or not self._settlement_current(source, generation)
                or source.supply_channel != "hub"
                or source.state.status in {"needs_action", "error"}
            ):
                return
            # A current success outranks every attempt admitted before it.
            self._latest_source_attempt_generation[source_id] = generation
            recovered = self.recovery.succeeded(source)
            if recovered and source.state.status == "cooldown":
                previous = self._clone_config(config)
                source.state = ModelHubSourceStateConfig(status="standby")
                try:
                    await self._save_runtime_config(previous, config)
                except OSError:
                    # Inference is authoritative; the policy remembers exactly
                    # which stale persisted cooldown must not be imported again.
                    logger.warning("Could not persist Model Hub recovered state")
            if recovered:
                self._record_event(
                    agent=backend, kind="recover", model_id=model_id,
                    reason="recovery", to_source=source.id, to_label=source.display_name, now=self.now(),
                )

    @staticmethod
    def _verified_recovery_outcome(outcome: RawCallOutcome) -> bool:
        # Buffered compatibility admission also sets stream_started on unknown
        # HTTP 200 bodies. Only the protocol evidence bit proves success here;
        # actual streaming output is read separately from the handle observer.
        return outcome.kind is RawOutcomeKind.SUCCESS and outcome.recovery_verified

    async def _write_cooldown_locked(
        self,
        config: ModelHubConfig,
        source: ModelHubSourceConfig,
        decision: ResolutionDecision,
        *,
        agent: EventAgent,
        model_id: str,
        detail_key: Optional[str] = None,
        emit_event: bool = True,
        retry_at: datetime | None = None,
    ) -> bool:
        retry_at = retry_at or self.now() + timedelta(seconds=decision.cooldown_seconds)
        if (
            source.state.status == "cooldown"
            and source.state.retry_at is not None
            and parse_model_hub_timestamp(source.state.retry_at) >= retry_at
        ):
            return False
        previous = self._clone_config(config)
        already_cooling = source.state.status == "cooldown"
        source.state = ModelHubSourceStateConfig(
            status="cooldown",
            retry_at=retry_at.isoformat(),
            detail_key=detail_key or f"models.source.cooldown.{decision.reason}",
        )
        try:
            persisted = await self._save_runtime_config(previous, config)
        except OSError:
            # HTTP recovery already owns the live deadline. Failed telemetry
            # cannot replace the upstream result or erase temporary eligibility.
            logger.warning("Could not persist Model Hub cooldown state")
            persisted = False
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
        outcome: RawCallOutcome | None = None,
    ) -> tuple[EventReason, bool]:
        """Persist one fallback-class Source result before the turn settles."""

        if decision.reason is None:
            raise AssertionError("fallback-class outcome must retain its Source reason")
        event_reason = cast(EventReason, decision.reason)
        settlement_rule = source_settlement_rule(event_reason)
        if decision.reason == "network" and outcome is not None and outcome.stream_started:
            return event_reason, False
        if (
            decision.reason == "unclassified_error"
            and outcome is not None
            and outcome.http_status is not None
            and 200 <= outcome.http_status < 300
        ):
            # An error frame no machine code names, inside a response the upstream
            # accepted, is ambiguous evidence: the relay, the engine, or a transient
            # fault. It ends this attempt and never writes Source health, but a
            # half-open attempt still gives its slot back.
            self.recovery.release(source.id, settlement_generation)
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
            self.recovery.release(source.id, generation)
            config = self.store.load()
            try:
                current = self._source(config, source.id)
            except ModelHubError:
                return event_reason, False
            if not self._settlement_current(current, generation):
                return event_reason, False
            if not source_settlement_allowed(current.state.status, event_reason):
                return event_reason, False
            if settlement_rule.status == "cooldown":
                retry_at = None
                changed = True
                if current.supply_channel == "hub":
                    retry_at, changed = self.recovery.failed(
                        current, event_reason,
                        generation=generation,
                        retry_after=outcome.retry_after if outcome is not None else None,
                        retry_after_ms=getattr(outcome, "retry_after_ms", None) if outcome is not None else None,
                        response_received_at=outcome.response_received_at if outcome is not None else None,
                    )
                if not changed:
                    return event_reason, False
                if not settlement_rule.may_write_health:
                    if emit_event:
                        self._record_event(
                            agent=cast(EventAgent, backend), kind="cooldown", model_id=model_id,
                            reason=event_reason, from_source=current.id,
                            from_label=current.display_name, now=self.now(),
                        )
                    return event_reason, False
                persisted = await self._write_cooldown_locked(
                    config,
                    current,
                    decision,
                    agent=cast(EventAgent, backend),
                    model_id=model_id,
                    detail_key=detail_key,
                    emit_event=emit_event,
                    retry_at=retry_at,
                )
            else:
                blocker_detail_key = detail_key or {
                    "credential_expired": "models.source.needs_action.oauth_expired",
                    "credential_revoked": "models.source.needs_action.credential_revoked",
                    "balance_exhausted": "models.source.needs_action.balance_exhausted",
                    "account_banned": "models.source.needs_action.account_banned",
                    "plan_excludes_product": "models.source.needs_action.plan_excludes_product",
                    "spend_limit_reached": "models.source.needs_action.spend_limit_reached",
                    "unclassified_error": "models.source.error.unclassified",
                }[event_reason]
                persisted = await self._write_source_blocker_locked(
                    config,
                    current,
                    backend=backend,
                    model_id=model_id,
                    detail_key=blocker_detail_key,
                    reason=event_reason,
                    emit_event=emit_event,
                )
            self.recovery.release(source.id, generation)
        return event_reason, persisted

    def _inspect_terminal_chain(
        self,
        *,
        backend: BackendName,
        model_id: str,
        config: ModelHubConfig | None = None,
    ) -> tuple[ModelHubConfig, ModelHubTurnResolution]:
        """Inspect the complete effective chain used by the next turn.

        ``config`` is a view of the stored config, for a backend that can take
        only part of what is stored; by default the stored config is inspected.
        """

        config = config if config is not None else self.store.load()
        resolution = resolve_model_hub_turn(
            config,
            backend,
            model_id,
            now=self.now(),
            unavailable_source_ids=self._unavailable_native_sources(
                config,
                backend,
            ),
            live_recovery=self.recovery_annotations(config),
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
            return produce_turn_outcome(
                "turn.request_nonfallback",
                upstream_detail=outcome.upstream_detail,
            )
        if category == "upstream_protocol":
            # The Gateway's existing protocol-error copy is the positive row;
            # a request-incompatible projection would misclassify the failure.
            return None
        if category == "engine_down":
            return replace(
                produce_turn_outcome("turn.engine_down", stream_started=outcome.stream_started),
                local_error_detail=format_os_errno(outcome.os_errno),
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
        source_label = next(
            (source.display_name for source in config.sources if source.id == source_id), source_id,
        )
        return produce_turn_outcome(
            "turn.streamed_fallback",
            config=config,
            resolution=resolution,
            attempted_hop=(source_id, source_model_id),
            source_transition_persisted=source_transition_persisted,
            last_upstream_failure=self._upstream_failure(source_label, outcome),
        )

    @staticmethod
    def _upstream_failure(source_label: str, outcome: RawCallOutcome) -> TurnUpstreamFailure | None:
        if not outcome.upstream_detail:
            return None
        if outcome.kind in {RawOutcomeKind.NETWORK_ERROR, RawOutcomeKind.TIMEOUT}:
            # The engine's own connection diagnostic: no upstream answered.
            return TurnUpstreamFailure(
                source=source_label,
                http_status=None,
                detail=outcome.upstream_detail,
                connection_error=True,
            )
        status = outcome.http_status
        return TurnUpstreamFailure(
            source=source_label,
            http_status=(
                status
                if type(status) is int and 300 <= status <= 599
                else None
            ),
            detail=outcome.upstream_detail,
        )

    @staticmethod
    def _produce_no_candidate_terminal_outcome(
        *,
        config: ModelHubConfig,
        resolution: ModelHubTurnResolution,
        last_upstream_failure: TurnUpstreamFailure | None = None,
    ) -> TurnOutcomeProjectionInput:
        return produce_turn_outcome(
            no_candidate_decision(config, resolution),
            config=config,
            resolution=resolution,
            last_upstream_failure=last_upstream_failure,
        )

    @staticmethod
    def _produce_exhausted_terminal_outcome(
        *,
        config: ModelHubConfig,
        resolution: ModelHubTurnResolution,
        last_upstream_failure: TurnUpstreamFailure | None = None,
    ) -> TurnOutcomeProjectionInput:
        return produce_turn_outcome(
            "turn.exhausted",
            config=config,
            resolution=resolution,
            last_upstream_failure=last_upstream_failure,
        )

    @staticmethod
    def _mark_source_unverified(source: ModelHubSourceConfig) -> None:
        # Persist identity with the credential, including same-handle reauth: the marker clears once a call verifies
        # the credential, and the generation keeps it, so the Source's identity never reverts to an earlier one.
        marker = f"vp_{uuid.uuid4().hex}"
        source.verification_pending = marker
        source.credential_generation = marker

    async def _verify_successful_source(
        self,
        source_id: str,
        credential_ref: str | None,
        verification_pending: str | None,
        outcome: RawCallOutcome,
    ) -> None:
        """Retire pending verification only for the current credential's call.

        The Source's identity does not include the marker, so retiring it changes no identity: health, retry state
        and evidence all carry over.
        """
        if outcome.kind is not RawOutcomeKind.SUCCESS or verification_pending is None:
            return

        def clear_current(config: ModelHubConfig) -> bool:
            current = next((item for item in config.sources if item.id == source_id), None)
            if (
                current is None
                or current.credential_ref != credential_ref
                or current.verification_pending != verification_pending
            ):
                return False
            current.verification_pending = None
            return True

        try:
            async with self._mutation_lock:
                # Held under the mutation lock until the write lands, even if the caller is cancelled.
                await run_owned_in_thread(self.store.mutate, clear_current)
            return
        except ValueError as exc:
            if "recovery warnings" not in str(exc):
                raise
            logger.warning("Could not persist Model Hub verification state during config recovery")
        except OSError:
            # Advisory bookkeeping must not turn a completed model call into a
            # failed response. Retain pending verification for the next success.
            logger.warning("Could not persist Model Hub verification state")

    async def settle_handle_outcome(
        self,
        resolved: ResolvedInvocation | None,
        outcome: RawCallOutcome | None,
        *,
        termination_origin: HandleTerminationOrigin,
        record_attempt: Callable[[RawCallOutcome, ResolutionDecision], None],
    ) -> HandleSettlement:
        """Settle every consumed hub handle before its terminal facts are exposed."""

        try:
            return await self._settle_handle_outcome(
                resolved, outcome, termination_origin=termination_origin, record_attempt=record_attempt,
            )
        finally:
            if resolved is not None:
                self.recovery.release(resolved.source_id, resolved.settlement_generation)

    async def _settle_handle_outcome(
        self,
        resolved: ResolvedInvocation | None,
        outcome: RawCallOutcome | None,
        *,
        termination_origin: HandleTerminationOrigin,
        record_attempt: Callable[[RawCallOutcome, ResolutionDecision], None],
    ) -> HandleSettlement:
        if resolved is not None and resolved.handle is not None:
            await self._observe_handle_recovery(
                resolved.source_id, resolved.settlement_generation, resolved.handle,
                backend=resolved.backend, model_id=resolved.requested_model_id,
            )
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
            if self._verified_recovery_outcome(outcome):
                await self._record_recovery_success(
                    resolved.source_id, resolved.settlement_generation,
                    backend=resolved.backend, model_id=resolved.requested_model_id,
                )
            await self._verify_successful_source(
                resolved.source_id, resolved.credential_ref, resolved.verification_pending, outcome,
            )
            await self._record_native_successes(resolved, outcome)
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
                    outcome=outcome,
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

    async def _record_native_successes(self, resolved: ResolvedInvocation, outcome: RawCallOutcome) -> None:
        """Positive evidence for each natively declared tool the served response proves ran (§3.2).

        Display-only history, written after the response completed. It is
        keyed on the identity the hop was resolved under: a Source changed
        since is not vouched for.
        """

        proven = resolved.hosted_calls & outcome.hosted_tools
        if not proven or resolved.origin is None:
            return
        # The config grows with use: read it off the controller loop.
        config = await asyncio.to_thread(self.store.load)
        source = next((item for item in config.sources if item.id == resolved.source_id), None)
        if source is None or identity_digest(source) != resolved.origin.identity:
            return
        for tool in sorted(proven):
            await self.record_hosted_success(source, resolved.model_id, tool, "native")

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

    def _invocation_resolution(
        self,
        config: ModelHubConfig,
        backend: BackendName,
        model_id: str,
        supply_channel: Literal["hub"] | None = None,
    ) -> ModelHubTurnResolution:
        if config.agents[backend].mode != "hub":
            # Only Hub turns reach the gateway. One admitted before a switch to
            # Direct keeps resolving its routes until it ends; new turns launch
            # Direct, so they never get here.
            config = self._clone_config(config)
            config.agents[backend].mode = "hub"
        resolution = resolve_model_hub_turn(
            config,
            backend,
            model_id,
            now=self.now(),
            unavailable_source_ids=self._unavailable_native_sources(config, backend),
            supply_channel=supply_channel,
            live_recovery=self.recovery_annotations(config),
        )
        if resolution.channel == "direct":
            raise ModelHubError("mapping_target_unavailable", status=409)
        return resolution

    async def _invoke_admitted(
        self,
        *,
        source: ModelHubSourceConfig,
        model_id: str,
        requested_model_id: str,
        request: Mapping[str, Any],
        stream: bool,
        backend: BackendName,
        excluded_source_ids: set[str],
        excluded_hops: set[tuple[str, str]] | None = None,
        supply_channel: Literal["hub"] | None = None,
        exact_retry: bool = False,
        on_admitted: Callable[[int], None] | None = None,
        recovery_request: RecoveryRequest | None = None,
        hosted: HostedContent = HostedContent(),
    ) -> InvokeHandle:
        while True:
            await self._mutation_lock.acquire()
            held = True
            generation: int | None = None
            admission_confirmed = False

            def release_exclusion() -> None:
                nonlocal held
                if held:
                    held = False
                    self._mutation_lock.release()

            def admitted() -> None:
                nonlocal admission_confirmed
                admission_confirmed = True
                release_exclusion()
                if on_admitted is not None:
                    on_admitted(generation)

            try:
                config = self.store.load()
                resolution = self._invocation_resolution(config, backend, requested_model_id, supply_channel)
                pinned_hop = _pinned_hop(request, backend)
                candidates = [
                    hop for hop in resolution.candidate_hops
                    if hop.source_id not in excluded_source_ids
                    and (hop.source_id, hop.model_id) not in (excluded_hops or ())
                    and (pinned_hop is None or (hop.source_id, hop.model_id) == pinned_hop)
                ]
                candidate = next(
                    (
                        hop for hop in candidates
                        if not exact_retry or (hop.source_id, hop.model_id) == (source.id, model_id)
                    ),
                    None,
                )
                if (
                    candidate is None
                    or candidate.source != source
                    or candidate.model_id != model_id
                ):
                    raise _InvocationPlanChanged
                attempt_request = request
                if backend == "vibey":
                    reason = _google_admission_skip_reason(
                        getattr(request, "protocol", None), source.protocol, model_id, stream,
                    )
                    if reason is not None:
                        raise _InvocationUnsupported(reason)
                    # Validate the exact rechecked admission snapshot for every
                    # hop, including fallback/refresh. Persisted ids stay intact;
                    # refusal must precede a transport, attempt, or recovery slot.
                    try:
                        origin = HopOrigin(source.vendor, source.protocol, model_id, source.id)
                        origin.response_headers()
                    except ServedHopHeaderTooLarge:
                        raise ModelHubError("served_hop_too_large", status=422) from None
                    # Opaque payloads are bound to the account that issued them,
                    # the Source identity, not only to the origin (C-6 item 6). The
                    # request was rendered for the launch primary's identity; the
                    # serving one is read on this final snapshot, so a key rotated
                    # while the engine was prepared counts.
                    primary = getattr(request, "primary_origin", None)
                    identity = identity_digest(source)
                    other_account = (
                        primary != origin or primary.source != origin.source or primary.identity != identity
                    )
                    if pinned_hop is None and other_account:
                        # An unpinned request is stripped for any other account.
                        attempt_request = without_opaque_history(request)
                    elif (
                        other_account
                        and primary is not None
                        and pinned_hop == (primary.source, primary.model)
                    ):
                        # A pinned request is never rewritten: rendered for the
                        # primary's identity, it cannot go to the primary under
                        # another, so Vibey renders it again portably.
                        raise _pin_unavailable()
                    # Hosted content is admitted on this final snapshot, so evidence
                    # or a Source that changed while the engine was prepared counts.
                    if hosted and not self._admits_hosted(hosted, source, model_id):
                        raise _pin_unavailable()
                    # Native content stays on one identity of the Source per turn. The
                    # claim is made here, under the exclusion this call is admitted in,
                    # so no concurrent request or config change slips between them.
                    claim = getattr(request, "pin_identity_claim", None)
                    if hosted and (claim is None or not claim(identity, bool(hosted.in_flight))):
                        raise _pin_unavailable()
                if self._engine_synced:
                    if recovery_request is not None and recovery_request.expired:
                        raise _RecoveryWindowClosed
                    generation = self._reserve_settlement_generation(source.id)
                    if not self.recovery.claim(source, generation):
                        raise _InvocationPlanChanged
                    # Lock order matches config sync. The adapter hands exclusion
                    # back only after owning the transport that sync must drain.
                    return await self._engine_call(self.adapter.invoke(
                        source.id, model_id, attempt_request, stream, backend, on_admitted=admitted,
                    ))
            except BaseException:
                self.recovery.release(source.id, generation)
                raise
            finally:
                if not admission_confirmed:
                    # A completed local engine failure may return without a
                    # transport or on_admitted. It owns neither an attempt nor
                    # a half-open slot after this call, even on normal return.
                    self.recovery.release(source.id, generation)
                release_exclusion()
            # A canceled configuration transaction left reconciliation pending.
            # Use its existing owner outside the lock, then revalidate the plan.
            await self._prepare_engine_for_demand()

    async def _invoke(
        self,
        *,
        source: ModelHubSourceConfig,
        model_id: str,
        request: Mapping[str, Any],
        stream: bool,
        backend: str,
        requested_model_id: str,
        excluded_source_ids: set[str],
        excluded_hops: set[tuple[str, str]] | None = None,
        supply_channel: Literal["hub"] | None = None,
        exact_retry: bool = False,
        on_admitted: Callable[[int], None] | None = None,
        recovery_request: RecoveryRequest | None = None,
        hosted: HostedContent = HostedContent(),
    ) -> tuple[InvokeHandle, Optional[RawCallOutcome], asyncio.CancelledError | None]:
        acquired_handle: InvokeHandle | None = None
        generation: int | None = None

        def admitted(value: int) -> None:
            nonlocal generation
            generation = value
            if on_admitted is not None:
                on_admitted(value)

        async def meter_observed(
            observed: ProtocolSSEState | None,
            outcome: RawCallOutcome | None,
        ) -> None:
            await self._meter_call(
                source_id=source.id,
                model_id=model_id,
                outcome=outcome,
                observed=observed,
            )

        async def meter_available_outcome(
            handle: InvokeHandle,
        ) -> RawCallOutcome | None:
            if handle.stream is not None and not handle.outcome_available:
                return None
            outcome = await self._engine_call(handle.outcome())
            await meter_observed(handle.observed, outcome)
            return outcome

        async def invoke_and_meter_bodyless() -> tuple[InvokeHandle, Optional[RawCallOutcome]]:
            nonlocal acquired_handle
            try:
                handle = await self._invoke_admitted(
                    source=source,
                    model_id=model_id,
                    requested_model_id=requested_model_id,
                    request=request,
                    stream=stream,
                    backend=cast(BackendName, backend),
                    excluded_source_ids=excluded_source_ids,
                    excluded_hops=excluded_hops,
                    supply_channel=supply_channel,
                    exact_retry=exact_retry,
                    on_admitted=admitted,
                    recovery_request=recovery_request,
                    hosted=hosted,
                )
            except InvokeCancelledError as cancelled:
                await meter_observed(cancelled.observed, None)
                raise
            acquired_handle = handle
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
            # Upstream inference is cancellable even after transport admission.
            # Only an acquired handle's finite settlement must outlive its caller.
            if acquired_handle is None:
                attempt_task.cancel()
            try:
                handle, outcome = await await_owned_task(attempt_task)
            except BaseException:
                self.recovery.release(source.id, generation)
                raise caught

        if cancelled is not None and handle.stream is not None:
            async def close_and_meter_observed_stream() -> RawCallOutcome | None:
                await handle.close_stream()
                outcome = await meter_available_outcome(handle)
                if outcome is None:
                    await meter_observed(handle.observed, None)
                return outcome

            cleanup_task = asyncio.create_task(close_and_meter_observed_stream())
            try:
                outcome = await await_owned_task(cleanup_task)
            except BaseException:
                self.recovery.release(source.id, generation)
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
                HopOrigin(source.vendor, source.protocol, source_model_id, source.id) if backend == "vibey" else None,
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
                outcome=outcome,
            )
        elif decision.action == "return" and self._verified_recovery_outcome(outcome):
            await self._record_recovery_success(
                source.id, settlement_generation, backend=backend, model_id=requested_model_id,
            )
        self.recovery.release(source.id, settlement_generation)

    async def _meter_call(
        self,
        *,
        source_id: str,
        model_id: str,
        outcome: RawCallOutcome | None,
        observed: ProtocolSSEState | None,
        at: datetime | None = None,
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

        ``at`` is when the call ended, when its caller settles it later than that.
        """

        usage = _call_usage(outcome, observed)
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
                at=at or self.now(),
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

    async def resolve(
        self,
        *,
        backend: str,
        model_id: str,
        request: Mapping[str, Any],
        stream: bool = False,
        supply_channel: Literal["hub"] | None = None,
        attempt_observer: Optional[AttemptObserver] = None,
        recovery_request: RecoveryRequest | None = None,
    ) -> ResolvedInvocation:
        if backend not in MODEL_HUB_BACKENDS:
            raise ModelHubError("mapping_target_unavailable")
        pinned_hop = _pinned_hop(request, backend)
        hosted = (
            # A walk of the whole history, so off the controller loop.
            await asyncio.to_thread(
                hosted_content, request, getattr(request, "protocol", ""), pinned=pinned_hop is not None,
            )
            if backend == "vibey"
            else HostedContent()
        )
        if hosted and pinned_hop is None:
            # Vibey pins every request carrying Source-specific hosted content,
            # so unpinned content has no correct hop: refuse it before anything runs.
            raise ModelHubError(
                "hosted_content_unpinned", status=422, turn_outcome=REQUEST_NONFALLBACK_TURN_OUTCOME,
            )
        engine_prepared = False
        # An entirely unsupported Google request must not reconcile the engine
        # or custody. Supported hops drain the journal in demand preparation.
        google_preflight = backend == "vibey" and getattr(request, "protocol", None) == "google"
        if not google_preflight and self.revocations.list():
            try:
                await self._ensure_engine_synced()
            except ModelHubError:
                # Cleanup remains durable and must not invent a supply failure.
                pass
            else:
                engine_prepared = True
        async with self._mutation_lock:
            config = self.store.load()
            resolution = self._invocation_resolution(
                config, cast(BackendName, backend), model_id, supply_channel,
            )
        event_agent = cast(EventAgent, backend)
        candidate_hops = list(resolution.candidate_hops)
        if pinned_hop is not None and all((hop.source_id, hop.model_id) != pinned_hop for hop in candidate_hops):
            # Cooling down, removed, or no longer this route's hop: refused
            # before anything is invoked, and never waited out.
            raise _pin_unavailable()
        if not candidate_hops:
            projection_config, projection_resolution = config, resolution
            if supply_channel is not None:
                projection_config, projection_resolution = self._inspect_terminal_chain(
                    backend=cast(BackendName, backend),
                    model_id=model_id,
                )
            # A request that attempted in an earlier walk exhausted those attempts.
            attempted = recovery_request is not None and recovery_request.attempt_count > 0
            turn_outcome = (
                self._produce_exhausted_terminal_outcome
                if attempted
                else self._produce_no_candidate_terminal_outcome
            )(
                config=projection_config,
                resolution=projection_resolution,
                last_upstream_failure=(
                    recovery_request.last_upstream_failure if recovery_request is not None else None
                ),
            )
            facts = turn_outcome.supply_facts
            if facts is None:
                raise AssertionError("no-candidate outcome must carry supply facts")
            expired = recovery_request is not None and recovery_request.expired
            raise ModelHubError(
                RECOVERY_EXHAUSTED_CODE if expired else "mapping_target_unavailable",
                status=409,
                supply_state=facts.supply_state,
                data=_recovery_cause(recovery_request.reason) if expired else None,
                blockers=exact_hop_blockers(projection_resolution),
                turn_outcome=turn_outcome,
            )

        failed_source: Optional[ModelHubSourceConfig] = None
        failed_reason: Optional[EventReason] = None
        last_upstream_failure = (
            recovery_request.last_upstream_failure if recovery_request is not None else None
        )
        window_closed = False
        non_retryable_failure = False
        attempted = False
        globally_blocked_source_ids: set[str] = set()
        unsupported_hops: set[tuple[str, str]] = set()
        unsupported_reason: str | None = None
        effort_rejected_sources: list[str] = []
        while True:
            async with self._mutation_lock:
                if recovery_request is not None and recovery_request.expired:
                    window_closed = not non_retryable_failure
                    break
                config = self.store.load()
                resolution = self._invocation_resolution(
                    config, cast(BackendName, backend), model_id, supply_channel,
                )
                inspection = next(
                    (
                        hop for hop in resolution.candidate_hops
                        if hop.source_id not in globally_blocked_source_ids
                        and (hop.source_id, hop.model_id) not in unsupported_hops
                        and (pinned_hop is None or (hop.source_id, hop.model_id) == pinned_hop)
                    ),
                    None,
                )
            if inspection is None:
                break
            source = inspection.source
            target_model = inspection.model_id
            if source is None or target_model is None:
                raise AssertionError("runnable hop must have an exact identity")
            verification_pending = source.verification_pending
            origin = (
                HopOrigin(source.vendor, source.protocol, target_model, source.id, identity_digest(source))
                if backend == "vibey" else None
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
                    backend=cast(BackendName, backend),
                    requested_model_id=model_id,
                    source_id=source.id,
                    source_label=source.display_name,
                    model_id=target_model,
                    handle=None,
                    outcome=None,
                    supply_channel="native_cli",
                )
            settlement_generation = None

            def admitted(generation: int) -> None:
                nonlocal attempted, settlement_generation
                attempted = True
                settlement_generation = generation
                if recovery_request is not None:
                    recovery_request.attempt_count += 1
                    recovery_request.source_id = source.id
                    recovery_request.publish("attempting")
                if attempt_observer is not None:
                    attempt_observer(
                        source.id,
                        target_model,
                        "hub",
                        False,
                        None,
                        None,
                        (),
                        (),
                        origin,
                    )

            try:
                if backend == "vibey":
                    reason = _google_admission_skip_reason(
                        getattr(request, "protocol", None), source.protocol, target_model, stream,
                    )
                    if reason is not None:
                        raise _InvocationUnsupported(reason)
                if self._effort_refused(source, target_model, request):
                    effort_rejected_sources.append(source.id)
                    raise _InvocationUnsupported("effort_unsupported")
                if pinned_hop is not None and getattr(request, "protocol", None) != source.protocol:
                    # Native content is never translated: the pinned hop must speak the request's protocol.
                    raise _pin_unavailable()
                await self._prepare_engine_for_demand(already_synced=engine_prepared)
                engine_prepared = True
                handle, outcome, cancelled = await self._invoke(
                    source=source,
                    model_id=target_model,
                    request=request,
                    stream=stream,
                    backend=backend,
                    requested_model_id=model_id,
                    excluded_source_ids=globally_blocked_source_ids,
                    excluded_hops=unsupported_hops,
                    supply_channel=supply_channel,
                    on_admitted=admitted,
                    recovery_request=recovery_request,
                    hosted=hosted,
                )
            except _InvocationUnsupported as skipped:
                if pinned_hop is not None:
                    # Normal routing would skip this hop; a pinned request cannot.
                    raise _pin_unavailable() from None
                unsupported_reason = skipped.reason
                unsupported_hops.add((source.id, target_model))
                logger.info(
                    "Model Hub skipped unsupported hop: %s", skipped.reason,
                    extra={"reason": skipped.reason, "source_id": source.id, "model_id": target_model},
                )
                continue
            except _InvocationPlanChanged:
                continue
            except _RecoveryWindowClosed:
                window_closed = not non_retryable_failure
                break
            if outcome is None:
                if cancelled is not None:
                    self.recovery.release(source.id, settlement_generation)
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
                    verification_pending=verification_pending,
                    origin=origin,
                    hosted_calls=frozenset(tool for tool, _type in hosted.calls),
                )
            decision = await self._classify_source_outcome(source, outcome)
            learned = await self._learn_capability_evidence(source, target_model, request, outcome)
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
                self.recovery.release(source.id, settlement_generation)
                # The engine refreshes its credential internally; L2 retries the
                # exact same source once and never falls through on a second 401.
                try:
                    handle, outcome, cancelled = await self._invoke(
                        source=source,
                        model_id=target_model,
                        request=request,
                        stream=stream,
                        backend=backend,
                        requested_model_id=model_id,
                        excluded_source_ids=globally_blocked_source_ids,
                        excluded_hops=unsupported_hops,
                        supply_channel=supply_channel,
                        exact_retry=True,
                        on_admitted=admitted,
                        recovery_request=recovery_request,
                        hosted=hosted,
                    )
                except _InvocationPlanChanged:
                    if attempt_observer is not None:
                        attempt_observer(source.id, target_model, "hub", False, outcome, decision, (), (), origin)
                    continue
                except _RecoveryWindowClosed:
                    window_closed = not non_retryable_failure
                    break
                if outcome is None:
                    if cancelled is not None:
                        self.recovery.release(source.id, settlement_generation)
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
                        verification_pending=verification_pending,
                        origin=origin,
                        hosted_calls=frozenset(tool for tool, _type in hosted.calls),
                    )
                decision = classify_outcome(outcome, refresh_attempted=True)
                learned = await self._learn_capability_evidence(source, target_model, request, outcome)
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
            if attempt_observer is not None and settlement_generation is not None:
                attempt_observer(
                    source.id,
                    target_model,
                    "hub",
                    False,
                    outcome,
                    decision,
                    (),
                    (),
                    origin,
                )
            if decision.action == "return":
                if self._verified_recovery_outcome(outcome):
                    await self._record_recovery_success(
                        source.id, settlement_generation, backend=cast(BackendName, backend), model_id=model_id,
                    )
                await self._verify_successful_source(
                    source.id, source.credential_ref, verification_pending, outcome,
                )
                self.recovery.release(source.id, settlement_generation)
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
                    verification_pending=verification_pending,
                    origin=origin,
                )
            if (
                pinned_hop is not None
                and decision.action == "surface"
                and not outcome.stream_started
                and learned is not None
                and learned.feature.kind == "hosted_tool"
            ):
                # A narrow hosted-content rule named this refusal and recorded
                # its evidence: the pinned hop cannot take this content, so the
                # request goes back to Vibey for its portable form.
                self.recovery.release(source.id, settlement_generation)
                raise _pin_unavailable(learned, source.id)
            if decision.action == "surface":
                self.recovery.release(source.id, settlement_generation)
                source_transition_persisted: bool | None = None
                if outcome.stream_started and decision.reason is not None:
                    source_transition_persisted = False
                    _reason, source_transition_persisted = await self._settle_fallback_source(
                        source,
                        decision,
                        backend=cast(BackendName, backend),
                        model_id=model_id,
                        settlement_generation=settlement_generation,
                        outcome=outcome,
                    )
                raise ModelHubError(
                    decision.error_code or outcome.error_code or "engine_down",
                    origin=(
                        origin if settlement_generation is not None
                        and terminal_outcome_category(outcome, decision) != "engine_down"
                        else None
                    ),
                    local_error_detail=format_os_errno(outcome.os_errno),
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
            if decision.action == "fallback" and pinned_hop is not None:
                # A failure normal routing would fail over from. The Source's
                # health is settled as usual; the request goes back to Vibey,
                # which re-renders it in portable form and sends it unpinned.
                await self._settle_fallback_source(
                    source,
                    decision,
                    backend=cast(BackendName, backend),
                    model_id=model_id,
                    settlement_generation=settlement_generation,
                    outcome=outcome,
                )
                raise _pin_unavailable()
            if decision.action == "fallback":
                non_retryable_failure |= decision.reason not in RETRY_DELAYS
                if recovery_request is not None and decision.reason in RETRY_DELAYS:
                    recovery_request.start()
                    recovery_request.reason = decision.reason
                elif recovery_request is not None:
                    recovery_request.non_retryable_failure = True
                # The newest refusal wins even without text: an older message
                # would name a response that did not end this request.
                last_upstream_failure = self._upstream_failure(source.display_name, outcome)
                if recovery_request is not None:
                    recovery_request.last_upstream_failure = last_upstream_failure
                event_reason, _persisted = await self._settle_fallback_source(
                    source,
                    decision,
                    backend=cast(BackendName, backend),
                    model_id=model_id,
                    settlement_generation=settlement_generation,
                    outcome=outcome,
                )
                globally_blocked_source_ids.add(source.id)
                failed_source = source
                failed_reason = event_reason
                continue
            raise ModelHubError(
                decision.error_code or "engine_down",
                status=502,
                turn_outcome=ENGINE_DOWN_TURN_OUTCOME,
                local_error_detail=format_os_errno(outcome.os_errno),
            )
        if pinned_hop is not None:
            # The pinned hop left the route or its recovery window closed.
            raise _pin_unavailable()
        if (
            unsupported_reason == "effort_unsupported"
            and failed_source is None
            and self._effort_capable_hop_may_recover(backend, model_id, supply_channel, request)
        ):
            # A hop that does not refuse this effort is only blocked for now:
            # the request waits for it like any other, rather than ending here.
            unsupported_reason = None
        if unsupported_reason is not None and failed_source is None:
            # No upstream attempt exists. Exhaustion would invent a failed
            # attempt (and a retryable supply state) for a local limitation.
            raise ModelHubError(
                unsupported_reason,
                status=422,
                turn_outcome=REQUEST_NONFALLBACK_TURN_OUTCOME,
                data=(
                    {
                        "effort": wire_effort(request, getattr(request, "protocol", "")),
                        "source_ids": list(dict.fromkeys(effort_rejected_sources)),
                    }
                    if unsupported_reason == "effort_unsupported"
                    else None
                ),
            )
        final_config, final_resolution = self._inspect_terminal_chain(
            backend=cast(BackendName, backend),
            model_id=model_id,
        )
        # A request that admitted nothing in any walk (another waiter held the
        # half-open slot, or the window closed first) found every hop blocked;
        # it exhausted no attempt of its own.
        attempted = attempted or (recovery_request is not None and recovery_request.attempt_count > 0)
        produce_outcome = (
            self._produce_exhausted_terminal_outcome
            if attempted
            else self._produce_no_candidate_terminal_outcome
        )
        turn_outcome = produce_outcome(
            config=final_config,
            resolution=final_resolution,
            last_upstream_failure=last_upstream_failure,
        )
        final_facts = turn_outcome.supply_facts
        if final_facts is None:
            raise AssertionError("exhausted outcome must carry supply facts")
        raise ModelHubError(
            RECOVERY_EXHAUSTED_CODE if window_closed else "mapping_target_unavailable",
            status=503,
            supply_state=final_facts.supply_state,
            data=(
                _recovery_cause(failed_reason or (recovery_request.reason if recovery_request is not None else None))
                if window_closed
                else None
            ),
            blockers=exact_hop_blockers(final_resolution),
            turn_outcome=turn_outcome,
        )

    async def _observe_handle_recovery(
        self, source_id: str, generation: int | None, handle: InvokeHandle,
        *, backend: BackendName, model_id: str,
    ) -> None:
        observed = handle.observed
        proved = observed is not None and observed.model_output_started
        if proved:
            await self._record_recovery_success(
                source_id, generation, backend=backend, model_id=model_id,
            )

    async def resolve_with_recovery(
        self, *, recovery_observer: Callable[[dict | None], None] | None = None, **kwargs,
    ) -> ResolvedInvocation:
        """One pending model request, including all fallback passes and waits."""

        if self.recovery.window_seconds == 0:
            # Explicit opt-out for single-walk probes and characterization tests.
            return await self.resolve(**kwargs)
        pending = RecoveryRequest(self.recovery, observer=recovery_observer)
        supply_channel = kwargs.get("supply_channel")
        try:
            while True:
                try:
                    return await self.resolve(**kwargs, recovery_request=pending)
                except ModelHubError as exc:
                    projection = exc.turn_outcome
                    if (
                        projection is None or projection.outcome not in {"no_candidate", "exhausted"}
                        or projection.supply_facts is None
                        or pending.non_retryable_failure
                    ):
                        raise
                    pending.start()
                    if pending.expired:
                        raise self._recovery_exhausted(exc, pending.reason) from exc
                    config = self.store.load()
                    resolution = self._invocation_resolution(
                        config, kwargs["backend"], kwargs["model_id"], supply_channel,
                    )
                    request = kwargs.get("request")
                    if any(
                        hop.source is not None
                        and hop.model_id is not None
                        and not self._effort_refused(hop.source, hop.model_id, request)
                        for hop in resolution.candidate_hops
                    ):
                        # Retry now only for a hop this request may use: a hop
                        # that refuses its effort would spin the loop.
                        continue
                    # An action already owed elsewhere in the chain keeps it
                    # `interrupted` for display, but must not end this request
                    # while another hop may still heal.
                    waitable = resolution.self_healing_hops(supply_channel)
                    if not waitable:
                        raise
                    annotations = self.recovery.annotations(config)
                    waits = [
                        (
                            max(0.0, (parse_model_hub_timestamp(hop.retry_at) - self.now()).total_seconds()),
                            hop,
                        )
                        for hop in waitable
                        if hop.recovery != "in_flight" and hop.retry_at
                    ]
                    selected = min(waits, key=lambda item: item[0]) if waits else None
                    delay = selected[0] if selected is not None else pending.remaining
                    hop = selected[1] if selected is not None else waitable[0]
                    pending.source_id = hop.source_id
                    annotation = annotations.get(hop.source_id)
                    pending.reason = annotation.reason if annotation else pending.reason
                    if delay >= pending.remaining - RECOVERY_ADMISSION_MARGIN_SECONDS:
                        # Another admitted owner can still wake this request early.
                        if not any(hop.recovery == "in_flight" for hop in waitable):
                            raise self._recovery_exhausted(exc, pending.reason) from exc
                        delay = pending.remaining
                    changed = self.recovery.changed
                    pending.publish("waiting", next_eligible_at=hop.retry_at)
                    await self.recovery.wait(delay, changed)
                    if pending.expired:
                        # Preserve this request's admission-expiry cause even
                        # if an owner succeeded or configuration changed on wake.
                        raise self._recovery_exhausted(exc, pending.reason) from exc
        finally:
            pending.clear()

    @staticmethod
    def _recovery_exhausted(error: ModelHubError, reason: str | None) -> ModelHubError:
        # The walk's own failure is the most recent one; otherwise the reason the request was waiting out.
        return ModelHubError(
            RECOVERY_EXHAUSTED_CODE, status=error.status,
            supply_state=error.supply_state, blockers=error.blockers,
            data=error.data if error.code == RECOVERY_EXHAUSTED_CODE and error.data else _recovery_cause(reason),
            turn_outcome=error.turn_outcome,
        )


def _recovery_cause(reason: str | None) -> dict[str, str] | None:
    """The failure an exhausted recovery waited out, for the caller to classify (``classification.py`` reasons)."""

    return {"cause": reason} if reason else None


def create_default_service(
    *,
    adapter: Optional[EngineAdapter] = None,
    native_oauth_adapter: Optional[NativeOAuthAdapter] = None,
    requested_model_override: Optional[Callable[[BackendName], Optional[str]]] = None,
    selected_agent_override: Optional[Callable[[BackendName], Optional[str]]] = None,
    named_agents_override: Optional[
        Callable[[BackendName], list[tuple[str, Optional[str]]]]
    ] = None,
    builtin_agent_models_override: Optional[
        Callable[[], list[tuple[BackendName, str]]]
    ] = None,
    cli_present_override: Optional[Callable[[BackendName], bool]] = None,
    cli_presence_refresh: Optional[
        Callable[[bool, tuple[BackendName, ...] | None], None]
    ] = None,
    backend_catalog_changed: Optional[
        Callable[[BackendName], Awaitable[None]]
    ] = None,
    repair_model_selections: Optional[
        Callable[[frozenset[str]], Awaitable[int]]
    ] = None,
) -> ModelHubService:
    if adapter is None:
        from vibe.model_hub_runtime import get_model_hub_engine_adapter

        adapter = get_model_hub_engine_adapter()

    if native_oauth_adapter is None:
        from .native_oauth import create_native_oauth_adapter

        native_oauth_adapter = create_native_oauth_adapter()

    def migration_project_roots() -> tuple[Path, ...]:
        # This owner-only operation covers registered local projects and the
        # default workdir, never an unbounded home/filesystem walk.
        roots: set[Path] = set()
        try:
            config = V2Config.load()
        except FileNotFoundError:
            pass
        else:
            if config.runtime.default_cwd:
                roots.add(Path(config.runtime.default_cwd).expanduser().resolve())
        if paths.get_sqlite_state_path().exists():
            from storage.db import get_cached_sqlite_engine
            from storage.projects_service import list_projects
            from vibe.authorization import instance_owner_context

            with get_cached_sqlite_engine().connect() as connection:
                projects = list_projects(
                    connection, include_archived=True,
                    authorization_context=instance_owner_context(),
                )
            roots.update(
                Path(project["folder_path"]).expanduser().resolve()
                for project in projects if project.get("folder_path")
            )
        return tuple(sorted(roots))

    return ModelHubService(
        store=V2ModelHubConfigStore(),
        adapter=adapter,
        events=BoundedEventLog(paths.get_state_dir() / "model_hub_resolution_events.json"),
        usage=BoundedUsageLedger(paths.get_state_dir() / "model_hub_usage.json"),
        native_oauth_adapter=native_oauth_adapter,
        oauth_flows=OAuthFlowRegistry(paths.get_state_dir() / "model_hub_oauth_flows.json"),
        revocations=CredentialRevocationJournal(paths.get_state_dir() / "model_hub_pending_revocations.json"),
        migration_project_roots=migration_project_roots,
        requested_model_override=requested_model_override,
        selected_agent_override=selected_agent_override,
        named_agents_override=named_agents_override,
        builtin_agent_models_override=builtin_agent_models_override,
        cli_present_override=cli_present_override,
        cli_presence_refresh=cli_presence_refresh,
        backend_catalog_changed=backend_catalog_changed,
        repair_model_selections=repair_model_selections,
    )
