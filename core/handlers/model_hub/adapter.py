"""Model Hub EngineAdapter interface. FINAL CONTRACT v7 (2026-09-03).

This file is the canonical adapter boundary and must remain byte-identical to
``core/handlers/model_hub/adapter.py``. The adapter owns one-Source operations:
credential custody, protocol observation, model discovery, OAuth, invocation,
and cleanup. It never chooses or reorders Route hops, classifies cross-Source
fallthrough, or exposes credential material. Contract changes route through the
orchestrator and land with every affected consumer on the same tested head.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, AsyncIterator, Final, Literal, Mapping, Protocol, Sequence

from .stream_wire import ProtocolSSEState, ProtocolUsageReport

ENGINE_TRANSPORT_TIMEOUT_SECONDS: Final = 60.0
SOURCE_PROTOCOLS = ("anthropic", "openai_responses", "openai_chat")
OBSERVATION_OUTCOMES = (
    "observed",
    "ambiguous",
    "unreachable",
    "authentication_failed",
    "adapter_error",
    "timeout",
)
OBSERVATION_DISCOVERY_OUTCOMES = ("succeeded", "failed", "not_attempted")


class EngineHealth(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"
    NOT_STARTED = "not_started"
    NOT_INSTALLED = "not_installed"
    INSTALLING = "installing"


@dataclass(frozen=True)
class EngineStatus:
    health: EngineHealth
    installed_version: str | None
    verified: bool
    listen_host: str  # always "127.0.0.1"
    listen_port: int | None
    last_check_iso: str | None
    host_platform: str | None = None
    error_key: str | None = None


class RuntimePlatformUnsupportedError(RuntimeError):
    """The pinned managed runtime has no asset for the server host."""


@dataclass(frozen=True)
class SourceBinding:
    """Engine-side registration of one hub-channel source (projection of config)."""

    source_id: str
    vendor: str
    protocol: str  # "anthropic" | "openai_responses" | "openai_chat"
    base_url: str | None  # None => vendor official default
    credential_ref: str  # opaque handle; never secret material
    allowed_origins: tuple[str, ...]  # agent names allowed to draw on this
    # source. Empty tuple = unrestricted (api_key default). Subscription
    # sources MUST be non-empty (README invariant 3); L2 populates, L1
    # enforces as backstop.
    model_ids: tuple[str, ...]  # declared supply list (discovered + manual
    # custom entries); required by the engine's generic/API-key config. Bare
    # model ids, no provider prefix.


class OriginNotAllowedError(Exception):
    """Raised by the adapter when ``invoke(origin=...)`` violates the source's
    ``allowed_origins``. A programming/policy error — never converted into a
    ``RawCallOutcome`` and never triggers fallback."""


class RawOutcomeKind(str, Enum):
    SUCCESS = "success"
    HTTP_ERROR = "http_error"
    NETWORK_ERROR = "network_error"
    TIMEOUT = "timeout"
    PROTOCOL_ERROR = "protocol_error"  # malformed/unparseable upstream response


@dataclass(frozen=True)
class RawCallOutcome:
    """Uninterpreted per-call result. Classification is L2's job, not L1's."""

    kind: RawOutcomeKind
    http_status: int | None
    error_code: str | None  # upstream machine code if parseable, else None
    redacted_message: str | None  # L1 guarantees no credential material
    stream_started: bool
    model_id: str
    source_id: str
    error_type: str | None = None  # raw upstream error type, if present
    error_candidates: tuple[str, ...] = ()  # unsorted raw type/code candidates
    # Tokens the upstream reported for this call, when the response carried a
    # readable report. A call that settles before it can hand its body onward
    # would otherwise take its token report with it, and a vendor that reported
    # tokens billed for them whether or not the call ended well.
    usage: ProtocolUsageReport | None = None


class ObservationOutcome(str, Enum):
    OBSERVED = OBSERVATION_OUTCOMES[0]
    AMBIGUOUS = OBSERVATION_OUTCOMES[1]
    UNREACHABLE = OBSERVATION_OUTCOMES[2]
    AUTHENTICATION_FAILED = OBSERVATION_OUTCOMES[3]
    ADAPTER_ERROR = OBSERVATION_OUTCOMES[4]
    TIMEOUT = OBSERVATION_OUTCOMES[5]


class ObservationDiscovery(str, Enum):
    SUCCEEDED = OBSERVATION_DISCOVERY_OUTCOMES[0]
    FAILED = OBSERVATION_DISCOVERY_OUTCOMES[1]
    NOT_ATTEMPTED = OBSERVATION_DISCOVERY_OUTCOMES[2]


@dataclass(frozen=True)
class DiscoveredModel:
    """One model inventory row and the only upstream metadata v1 retains."""

    id: str
    supported_parameters: tuple[str, ...] | None = None


@dataclass(frozen=True)
class SourceObservation:
    """Response-backed result of an unsaved Source observation."""

    outcome: ObservationOutcome
    reachable: bool | None
    authenticated: bool | None
    protocol: str | None
    discovery: ObservationDiscovery
    models: tuple[DiscoveredModel, ...]

    @property
    def model_ids(self) -> tuple[str, ...]:
        return tuple(model.id for model in self.models)


@dataclass(frozen=True)
class ObservationTerminalRule:
    """Complete legal product for one response-backed observation outcome."""

    reachable: frozenset[bool | None]
    authenticated: frozenset[bool | None]
    protocols: frozenset[str | None]
    discoveries: frozenset[ObservationDiscovery]
    models_must_be_empty: bool


OBSERVATION_TERMINAL_RULES: Mapping[
    ObservationOutcome,
    ObservationTerminalRule,
] = {
    ObservationOutcome.OBSERVED: ObservationTerminalRule(
        reachable=frozenset({True}),
        authenticated=frozenset({True}),
        protocols=frozenset(SOURCE_PROTOCOLS),
        discoveries=frozenset(
            {
                ObservationDiscovery.SUCCEEDED,
                ObservationDiscovery.FAILED,
            }
        ),
        models_must_be_empty=False,
    ),
    ObservationOutcome.AMBIGUOUS: ObservationTerminalRule(
        reachable=frozenset({True}),
        authenticated=frozenset({True, None}),
        protocols=frozenset({None}),
        discoveries=frozenset({ObservationDiscovery.NOT_ATTEMPTED}),
        models_must_be_empty=True,
    ),
    ObservationOutcome.UNREACHABLE: ObservationTerminalRule(
        reachable=frozenset({False}),
        authenticated=frozenset({None}),
        protocols=frozenset({None}),
        discoveries=frozenset({ObservationDiscovery.NOT_ATTEMPTED}),
        models_must_be_empty=True,
    ),
    ObservationOutcome.AUTHENTICATION_FAILED: ObservationTerminalRule(
        reachable=frozenset({True}),
        authenticated=frozenset({False}),
        protocols=frozenset({None}),
        discoveries=frozenset({ObservationDiscovery.NOT_ATTEMPTED}),
        models_must_be_empty=True,
    ),
    ObservationOutcome.ADAPTER_ERROR: ObservationTerminalRule(
        reachable=frozenset({True, None}),
        authenticated=frozenset({None}),
        protocols=frozenset({None}),
        discoveries=frozenset({ObservationDiscovery.NOT_ATTEMPTED}),
        models_must_be_empty=True,
    ),
    ObservationOutcome.TIMEOUT: ObservationTerminalRule(
        reachable=frozenset({None}),
        authenticated=frozenset({None}),
        protocols=frozenset({None}),
        discoveries=frozenset({ObservationDiscovery.NOT_ATTEMPTED}),
        models_must_be_empty=True,
    ),
}


def validate_source_observation(observation: object) -> SourceObservation:
    """Validate an adapter result against the sole terminal-product authority."""

    if not isinstance(observation, SourceObservation):
        raise TypeError("invalid SourceObservation")
    if not isinstance(observation.outcome, ObservationOutcome):
        raise ValueError("invalid SourceObservation outcome")
    if not isinstance(observation.discovery, ObservationDiscovery):
        raise ValueError("invalid SourceObservation discovery")
    if observation.reachable is not None and not isinstance(observation.reachable, bool):
        raise ValueError("invalid SourceObservation reachability")
    if observation.authenticated is not None and not isinstance(
        observation.authenticated,
        bool,
    ):
        raise ValueError("invalid SourceObservation authentication")
    if not isinstance(observation.models, tuple) or any(
        not isinstance(model, DiscoveredModel)
        or not isinstance(model.id, str)
        or not model.id
        or (
            model.supported_parameters is not None
            and (
                not isinstance(model.supported_parameters, tuple)
                or any(
                    not isinstance(parameter, str) or not parameter
                    for parameter in model.supported_parameters
                )
                or len(set(model.supported_parameters))
                != len(model.supported_parameters)
            )
        )
        for model in observation.models
    ):
        raise ValueError("invalid SourceObservation inventory")
    if len(set(observation.model_ids)) != len(observation.model_ids):
        raise ValueError("invalid SourceObservation inventory")

    rule = OBSERVATION_TERMINAL_RULES[observation.outcome]
    if (
        observation.reachable not in rule.reachable
        or observation.authenticated not in rule.authenticated
        or observation.protocol not in rule.protocols
        or observation.discovery not in rule.discoveries
        or (rule.models_must_be_empty and observation.model_ids)
        or (observation.discovery is ObservationDiscovery.FAILED and observation.model_ids)
    ):
        raise ValueError("invalid SourceObservation terminal product")
    return observation


def make_source_observation(
    *,
    outcome: ObservationOutcome,
    reachable: bool | None,
    authenticated: bool | None,
    protocol: str | None,
    discovery: ObservationDiscovery,
    models: Sequence[DiscoveredModel],
) -> SourceObservation:
    """Construct an adapter result through the terminal-product authority."""

    return validate_source_observation(
        SourceObservation(
            outcome=outcome,
            reachable=reachable,
            authenticated=authenticated,
            protocol=protocol,
            discovery=discovery,
            models=tuple(models),
        )
    )


class RetainedMaterialDisposition(str, Enum):
    NONE = "none"
    FLOW_SOURCE_REF = "flow_source_ref"
    ORPHAN_REF = "orphan_ref"
    FOREIGN_SOURCE_REF = "foreign_source_ref"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class OAuthFlowState:
    """Channel-aware subscription OAuth flow state.

    Deterministic binding: the flow is created FOR a pre-existing pending
    ``source_id``. On the Hub branch, ``credential_ref`` is non-null exactly
    on success and names the engine auth record L2 binds to that source. On the
    native CLI branch it is null at every state, including success, because the
    sanctioned CLI owns its login material. This split mirrors shipped truth in
    ``core/handlers/model_hub/native_oauth.py:252-289`` and
    ``tests/test_model_hub_api.py:1147-1149``.

    ``retained_material_disposition`` is total across every state. For Hub
    terminal states it reports where grant material written by THIS flow
    remains: ``none`` means cleanup was confirmed or nothing was written;
    ``flow_source_ref`` names the flow source's ref; ``orphan_ref`` retains a
    minted cleanup handle; ``foreign_source_ref`` means the single existing ref
    belongs to another source; and ``unknown`` is the corrupt/ambiguous-state
    floor, never a default. ``retained_credential_ref`` is non-null exactly for
    ``flow_source_ref`` and ``orphan_ref``. Foreign and unknown placements
    deliberately withhold a ref so consumers cannot act on another source or
    guess among candidates. Hub success pins ``flow_source_ref`` and equality
    of both refs. Native pins ``none`` and a null retained ref by construction.

    Mirrors ``oauth-flow.schema.json`` presentation semantics
    (runtime-declared; UI renders from ``expects``).
    """

    flow_id: str
    source_id: str
    vendor: str
    state: str  # "starting" | "awaiting_action" | "verifying" | "success" | "failed" | "cancelled"
    auth_url: str | None
    device_code: str | None
    expects: str  # "none" | "paste_code" | "paste_callback_url"
    instructions_key: str | None
    error_key: str | None
    expires_at_iso: str | None
    credential_ref: str | None
    # Channel-aware producers set all three fields explicitly. Defaults keep the
    # dataclass construction total for presentation-only adapter implementations.
    channel: Literal["hub", "native_cli"] = "hub"
    retained_material_disposition: RetainedMaterialDisposition = RetainedMaterialDisposition.NONE
    retained_credential_ref: str | None = None


class InvokeHandle(Protocol):
    """One in-flight upstream call.

    ``stream`` is None when a streaming failure settles before its first model
    output (the outcome is then immediately awaitable). When ``stream`` is not
    None, the settlement owner closes it before reading the outcome. A
    never-started stream may have no outcome after closing;
    ``outcome_available`` is the non-blocking guard.
    For streaming calls, ``stream_started`` becomes true only when the
    protocol taxonomy observes the first model output; transport metadata and
    error frames remain pre-output. ``close_stream()`` is idempotent.

    ``observed`` carries what the adapter itself read of this body, and it is
    the handle's only member that answers before the body is consumed. A
    streaming adapter has already read the head of the stream to decide there
    was one — Anthropic reports the input tokens it billed in that head — and it
    keeps reading as the body is yielded, so its tracker is never behind a
    consumer's. Without it the facts of a call would exist only in bytes the
    consumer has already pulled, and everything that can end a turn before the
    first pull would see a call that never happened. It is None only when the
    adapter tokenized no stream for this call.
    """

    @property
    def stream(self) -> AsyncIterator[bytes] | None: ...

    @property
    def observed(self) -> ProtocolSSEState | None: ...

    @property
    def outcome_available(self) -> bool: ...

    async def close_stream(self) -> None: ...

    async def outcome(self) -> RawCallOutcome: ...


class EngineAdapter(Protocol):
    # --- lifecycle -------------------------------------------------------
    async def install(self) -> EngineStatus: ...

    async def recover_installation(self) -> EngineStatus: ...

    async def ensure_installed(self) -> EngineStatus: ...

    async def start(self) -> EngineStatus: ...

    async def stop_runtime(self) -> EngineStatus: ...

    async def stop(self) -> None: ...

    async def status(self) -> EngineStatus: ...

    # --- gateway ---------------------------------------------------------
    async def gateway_token(self) -> str:
        """Local gateway token for Hub-channel backend injection.

        This is the only credential the managed engine injects. Native-channel
        turns bypass this gateway and use the sanctioned CLI's own login.
        """
        ...

    # --- credential provisioning (engine-owned store) ---------------------
    async def provision_credential(
        self,
        vendor: str,
        protocol: str,
        secret: str,
        base_url: str | None,
    ) -> str:
        """Store an API-key secret in the ENGINE-OWNED credential store and
        return the opaque ``credential_ref``.

        ``secret`` is transient: the adapter must never log it; L2 must never
        persist it (config stores refs only). Hub OAuth credentials never pass
        through here — they are created engine-side by the OAuth flow and
        surfaced via ``OAuthFlowState.credential_ref``. Native OAuth material
        remains CLI-owned and has no ref on this seam."""
        ...

    async def retarget_api_key_credential(
        self,
        credential_ref: str,
        vendor: str,
        protocol: str,
        base_url: str | None,
    ) -> str:
        """Copy an API-key credential to a fresh ref with a new target.

        The old ref remains valid until L2 commits the Source mutation and
        explicitly revokes it. The secret never crosses this adapter boundary,
        so Base URL replacement can be staged and rolled back transactionally.
        """
        ...

    async def credential_supports_refresh(self, credential_ref: str) -> bool:
        """Return the credential's actual engine-side refresh capability.

        This is a property of the stored credential, not an inference from
        vendor, Source kind, or an HTTP status.
        """
        ...

    async def revoke_credential(self, credential_ref: str) -> None:
        """Release the stored credential (source deletion / key replacement)."""
        ...

    async def provision_transient_credential(
        self,
        vendor: str,
        secret: str,
        base_url: str | None,
    ) -> str:
        """Provision an unbound credential for an unsaved observation."""
        ...

    async def cleanup_orphaned_oauth_material(self, credential_ref: str) -> bool:
        """Retry cleanup for an ``orphan_ref`` without exposing a filename.

        Return true only after the running-engine auth-file delete (when an
        engine is running), the local auth-file delete, and ref revocation are
        all confirmed, or when ``credential_ref`` no longer resolves. Absence
        is convergence, not a lenient fallback: this operation's ordering
        invariant revokes the ref only after both auth-file deletions are
        confirmed, so an absent ref proves that no material remains behind it.
        A never-created ref satisfies the same postcondition vacuously. Return
        false on any partial failure and retain the ref for a later retry. The
        handle must never be destroyed while material may remain behind it.
        """
        ...

    # --- source registry (L2 calls on every config change) ---------------
    async def sync_sources(self, bindings: Sequence[SourceBinding]) -> None: ...

    async def discover_models(
        self,
        vendor: str,
        protocol: str,
        base_url: str | None,
        credential_ref: str,
    ) -> Sequence[DiscoveredModel]:
        """Refresh supplyable models for a saved Source using its stored protocol."""
        ...

    async def observe_source(
        self,
        vendor: str,
        base_url: str | None,
        credential_ref: str,
        protocol_order: Sequence[str],
    ) -> SourceObservation:
        """Observe connectivity, authentication, protocol, and inventory.

        ``protocol_order`` either enumerates Auto-detect probes or names one
        owner-constrained protocol. A returned protocol may be established by
        a protocol-shaped upstream response, by a shipped API-key vendor pin,
        or by a concrete `custom` declaration. The latter two require a
        response from that exact path plus the September 4, 2026 auth ladder:
        401/403 reject, while 2xx and request-error 400/404/422 accept even
        if the response shape itself stays generic. `custom` Auto detect still
        requires response-backed proof; order alone never proves a protocol.
        """
        ...

    # --- subscription OAuth (channel-specific ownership) ------------------
    async def start_oauth(self, source_id: str, vendor: str) -> OAuthFlowState: ...

    async def oauth_status(self, flow_id: str) -> OAuthFlowState: ...

    async def submit_oauth(self, flow_id: str, value: str) -> OAuthFlowState:
        """``value`` per ``expects``: pasted code or callback URL."""
        ...

    async def cancel_oauth(self, flow_id: str) -> None: ...

    # --- invocation primitive (exactly one source; no engine fallback) ---
    async def invoke(
        self,
        source_id: str,
        model_id: str,
        request: Mapping[str, Any],
        stream: bool,
        origin: str,
    ) -> InvokeHandle:
        """``origin`` = requesting agent name ("claude"|"codex"|"opencode"|...).
        Raises ``OriginNotAllowedError`` when the binding's ``allowed_origins``
        excludes it (backstop; L2 must have filtered already)."""
        ...
