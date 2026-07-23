"""Typed boundary between Model Hub policy and the managed engine runtime.

The adapter deliberately returns raw outcomes.  Model Hub owns error
classification, candidate fallback, cooldowns, and redacted resolution events;
the engine implementation must not make those policy decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Optional, Protocol, Sequence

SourceKind = Literal["subscription", "api_key"]
SupplyChannel = Literal["native_cli", "hub"]
SourceProtocol = Literal[
    "anthropic",
    "openai_responses",
    "openai_chat",
    "openai_compatible",
]
RawFailureScope = Literal["upstream", "network", "engine"]
OAuthFlowState = Literal[
    "starting",
    "awaiting_action",
    "verifying",
    "success",
    "failed",
    "cancelled",
]
OAuthExpectation = Literal["none", "paste_code", "paste_callback_url"]


@dataclass(frozen=True)
class SecretInput:
    """Short-lived credential input that never exposes its value via ``repr``."""

    value: str = field(repr=False)


@dataclass(frozen=True)
class EngineSource:
    """Credential-free source projection consumed by the engine facade."""

    source_id: str
    kind: SourceKind
    vendor: str
    protocol: SourceProtocol
    supply_channel: SupplyChannel
    base_url: Optional[str]
    credential_ref: Optional[str]
    model_ids: Sequence[str]


@dataclass(frozen=True)
class ProvisionedSource:
    """Opaque credential handle and effective model list returned by the engine."""

    credential_ref: Optional[str]
    model_ids: Sequence[str]


@dataclass(frozen=True)
class SourceInvocation:
    """One adapter-controlled attempt against exactly one source."""

    source_id: str
    model_id: str
    request: Mapping[str, Any]
    stream: bool = False


@dataclass(frozen=True)
class RawInvocationOutcome:
    """Unclassified result of one source attempt.

    ``error_message`` and ``error_body`` are internal-only inputs to the policy
    classifier.  They must never be copied into API payloads, persisted events,
    or logs.  ``streaming_started`` is authoritative even when the attempt later
    fails, because transparent retry is forbidden after the first stream item.
    """

    status_code: Optional[int]
    response: Any = None
    failure_scope: Optional[RawFailureScope] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = field(default=None, repr=False)
    error_body: Optional[str] = field(default=None, repr=False)
    response_headers: Mapping[str, str] = field(default_factory=dict)
    streaming_started: bool = False


@dataclass(frozen=True)
class RawRefreshOutcome:
    """Raw result of the single refresh attempt allowed after a 401."""

    refreshed: bool
    status_code: Optional[int] = None
    failure_scope: Optional[RawFailureScope] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = field(default=None, repr=False)


@dataclass(frozen=True)
class OAuthFlowSnapshot:
    """Runtime-declared OAuth state; presentation is never inferred by vendor."""

    flow_id: str
    vendor: str
    channel: SupplyChannel
    state: OAuthFlowState
    expects: OAuthExpectation
    auth_url: Optional[str] = None
    device_code: Optional[str] = None
    instructions_key: Optional[str] = None
    error_key: Optional[str] = None
    expires_at: Optional[str] = None


class ModelHubEngineAdapter(Protocol):
    """Facade implemented by the L1 managed-runtime lane."""

    async def runtime_status(self) -> Mapping[str, Any]: ...

    async def provision_source(
        self,
        source: EngineSource,
        credential: Optional[SecretInput] = None,
    ) -> ProvisionedSource: ...

    async def update_source(self, source: EngineSource) -> ProvisionedSource: ...

    async def remove_source(self, source_id: str) -> None: ...

    async def discover_models(self, source: EngineSource) -> Sequence[str]: ...

    async def invoke_source(self, invocation: SourceInvocation) -> RawInvocationOutcome: ...

    async def refresh_source(self, source_id: str) -> RawRefreshOutcome: ...

    async def start_oauth(self, vendor: str, channel: SupplyChannel) -> OAuthFlowSnapshot: ...

    async def oauth_status(self, flow_id: str) -> OAuthFlowSnapshot: ...

    async def submit_oauth(self, flow_id: str, value: SecretInput) -> OAuthFlowSnapshot: ...

    async def cancel_oauth(self, flow_id: str) -> None: ...
