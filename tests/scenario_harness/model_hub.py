"""Reusable Model Hub scenario fixtures.

The harness keeps scenario tests at the service boundary while exposing enough
adapter telemetry to prove persistence, observation, and exact-hop behavior.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Iterable, Mapping

from config.v2_config import (
    ModelHubAgentSourcesConfig,
    ModelHubAgentSupplyConfig,
    ModelHubConfig,
    ModelHubModelConfig,
    ModelHubRouteConfig,
    ModelHubRouteHopConfig,
    ModelHubSourceConfig,
    ModelHubSourceStateConfig,
    ModelHubSourceUsageConfig,
    model_hub_fixed_menu_ids,
)
from core.handlers.model_hub.adapter import (
    DiscoveredModel,
    EngineHealth,
    EngineStatus,
    ObservationDiscovery,
    ObservationOutcome,
    RawCallOutcome,
    RawOutcomeKind,
    SourceObservation,
)
from core.handlers.model_hub.events import BoundedEventLog
from core.handlers.model_hub.provenance import BoundedProvenanceStore
from core.handlers.model_hub.revocations import CredentialRevocationJournal
from core.handlers.model_hub.service import ModelHubService


class MemoryModelHubStore:
    """In-memory persistence that still uses the production serializer boundary."""

    def __init__(self, config: ModelHubConfig) -> None:
        self.config = config
        self.saved_payloads: list[dict] = []

    def load(self) -> ModelHubConfig:
        return self.config

    def save(self, config: ModelHubConfig) -> None:
        self.config = ModelHubConfig.from_payload(config.to_payload())
        self.saved_payloads.append(self.config.to_payload())


@dataclass(frozen=True)
class ScenarioCallResult:
    kind: RawOutcomeKind
    status: int | None = None
    error_code: str | None = None
    body: bytes | None = None
    stream_started: bool = False


class ScenarioInvokeHandle:
    def __init__(self, outcome: RawCallOutcome, body: bytes | None) -> None:
        self._outcome = outcome
        self._body = body

    @property
    def stream(self) -> AsyncIterator[bytes] | None:
        if self._body is None:
            return None

        async def chunks() -> AsyncIterator[bytes]:
            yield self._body

        return chunks()

    @property
    def observed(self) -> None:
        """This double reports nothing about the body until its consumer reads it."""

        return None

    @property
    def outcome_available(self) -> bool:
        return True

    async def outcome(self) -> RawCallOutcome:
        return self._outcome

    async def close_stream(self) -> None:
        return None


class ModelHubScenarioAdapter:
    """Deterministic adapter double with observable call boundaries."""

    def __init__(
        self,
        *,
        observation: SourceObservation | None = None,
        discovery_models: Iterable[str] = (),
        refresh_models: Iterable[str] | None = None,
        invoke_results: Iterable[ScenarioCallResult] = (),
        refreshable_credential_refs: Iterable[str] = (),
    ) -> None:
        self.observation = observation or SourceObservation(
            outcome=ObservationOutcome.OBSERVED,
            reachable=True,
            authenticated=True,
            protocol="openai_chat",
            discovery=ObservationDiscovery.SUCCEEDED,
            models=tuple(
                DiscoveredModel(id=model_id) for model_id in discovery_models
            ),
        )
        self.discovery_models = tuple(discovery_models)
        self.refresh_models = tuple(refresh_models) if refresh_models is not None else self.discovery_models
        self.invoke_results = deque(invoke_results)
        self.refreshable_credential_refs = frozenset(refreshable_credential_refs)
        self.observation_calls: list[tuple[str, str | None, tuple[str, ...]]] = []
        self.discovery_calls: list[tuple[str, str, str | None, str]] = []
        self.provisioned_transient: list[str] = []
        self.provisioned: list[str] = []
        self.revoked: list[str] = []
        self.synced: list[tuple[object, ...]] = []
        self.invocations: list[tuple[str, str, str]] = []
        self.requests: list[Mapping[str, object]] = []

    async def ensure_installed(self) -> EngineStatus:
        return await self.status()

    async def start(self) -> EngineStatus:
        return await self.status()

    async def stop(self) -> None:
        return None

    async def status(self) -> EngineStatus:
        return EngineStatus(EngineHealth.OK, "scenario", True, "127.0.0.1", 18443, None)

    async def gateway_token(self) -> str:
        return "scenario-engine-token"

    async def provision_transient_credential(
        self,
        vendor: str,
        secret: str,
        base_url: str | None,
    ) -> str:
        ref = f"cred_observation_{len(self.provisioned_transient) + 1}"
        self.provisioned_transient.append(ref)
        return ref

    async def provision_credential(
        self,
        vendor: str,
        protocol: str,
        secret: str,
        base_url: str | None,
    ) -> str:
        ref = f"cred_source_{len(self.provisioned) + 1}"
        self.provisioned.append(ref)
        return ref

    async def revoke_credential(self, credential_ref: str) -> None:
        self.revoked.append(credential_ref)

    async def sync_sources(self, bindings) -> None:
        self.synced.append(tuple(bindings))

    async def observe_source(
        self,
        vendor: str,
        base_url: str | None,
        credential_ref: str,
        protocol_order,
    ) -> SourceObservation:
        self.observation_calls.append((vendor, base_url, tuple(protocol_order)))
        return self.observation

    async def discover_models(
        self,
        vendor: str,
        protocol: str,
        base_url: str | None,
        credential_ref: str,
    ) -> tuple[DiscoveredModel, ...]:
        self.discovery_calls.append((vendor, protocol, base_url, credential_ref))
        return tuple(DiscoveredModel(id=model_id) for model_id in self.refresh_models)

    async def credential_supports_refresh(self, credential_ref: str) -> bool:
        return credential_ref in self.refreshable_credential_refs

    async def invoke(
        self,
        source_id: str,
        model_id: str,
        request,
        stream: bool,
        origin: str,
    ) -> ScenarioInvokeHandle:
        self.invocations.append((source_id, model_id, origin))
        self.requests.append(request)
        result = self.invoke_results.popleft()
        outcome = RawCallOutcome(
            kind=result.kind,
            http_status=result.status,
            error_code=result.error_code,
            redacted_message=None,
            stream_started=result.stream_started,
            model_id=model_id,
            source_id=source_id,
        )
        return ScenarioInvokeHandle(outcome, result.body)


def fixed_model(backend: str) -> str:
    return model_hub_fixed_menu_ids(backend)[0]


def source_model(
    model_id: str,
    *,
    provenance: str = "discovered",
    reasoning_efforts: Iterable[str] = (),
    display_name: str | None = None,
) -> ModelHubModelConfig:
    return ModelHubModelConfig(
        id=model_id,
        provenance=provenance,
        reasoning_efforts=list(reasoning_efforts),
        display_name=display_name,
    )


def source(
    source_id: str,
    models: Iterable[str | ModelHubModelConfig],
    *,
    vendor: str = "anthropic",
    protocol: str = "anthropic",
    kind: str = "api_key",
    channel: str = "hub",
    status: str = "standby",
    credential_ref: str | None = None,
    retry_at: str | None = None,
) -> ModelHubSourceConfig:
    normalized_models = [item if isinstance(item, ModelHubModelConfig) else source_model(item) for item in models]
    if credential_ref is None and channel == "hub":
        credential_ref = f"cred_{source_id}"
    return ModelHubSourceConfig(
        id=source_id,
        kind=kind,
        vendor=vendor,
        display_name=source_id,
        protocol=protocol,
        supply_channel=channel,
        billing="monthly" if kind == "subscription" else "metered",
        state=ModelHubSourceStateConfig(status=status, retry_at=retry_at),
        usage=ModelHubSourceUsageConfig(),
        models=normalized_models,
        credential_ref=credential_ref,
    )


def config_with_sources(
    sources: Iterable[ModelHubSourceConfig],
    *,
    backend: str = "claude",
    menu_model: str | None = None,
    hops: Iterable[tuple[str, str]] | None = None,
) -> ModelHubConfig:
    source_items = list(sources)
    agents = {name: ModelHubAgentSupplyConfig.default(name, mode="hub") for name in ("claude", "codex", "opencode")}
    config = ModelHubConfig(sources=source_items, agents=agents)
    for name, agent in config.agents.items():
        eligible = [item.id for item in source_items if ModelHubConfig.source_eligible_for_backend(item, name)]
        agent.sources = ModelHubAgentSourcesConfig(order=eligible)
    selected_model = menu_model or fixed_model(backend)
    if hops is None:
        hops = tuple(
            (item.id, selected_model)
            for item in source_items
            if ModelHubConfig.source_eligible_for_backend(item, backend)
        )
    config.agents[backend].routes[selected_model] = ModelHubRouteConfig(
        hops=tuple(ModelHubRouteHopConfig(source_id, model_id) for source_id, model_id in hops)
    )
    return config


def service_for(
    tmp_path: Path,
    store: MemoryModelHubStore,
    adapter: ModelHubScenarioAdapter,
    *,
    now=None,
    requested_model_override=None,
) -> ModelHubService:
    state = tmp_path / "model-hub-state"
    return ModelHubService(
        store=store,
        adapter=adapter,
        events=BoundedEventLog(state / "events.json"),
        provenance=BoundedProvenanceStore(state / "provenance.json"),
        revocations=CredentialRevocationJournal(state / "revocations.json"),
        now=now or (lambda: datetime.now(timezone.utc)),
        requested_model_override=requested_model_override,
    )


def round_trip(config: ModelHubConfig) -> ModelHubConfig:
    return ModelHubConfig.from_payload(config.to_payload())
