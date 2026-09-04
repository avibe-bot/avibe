from __future__ import annotations

import asyncio
import hashlib
import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping
from unittest.mock import patch

import pytest

from config.v2_config import (
    ModelHubAgentSupplyConfig,
    ModelHubBackendModelConfig,
    ModelHubConfig,
    ModelHubMenuConfig,
    ModelHubModelConfig,
    ModelHubRouteConfig,
    ModelHubRouteHopConfig,
    ModelHubSourceConfig,
    ModelHubSourceStateConfig,
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
from core.handlers.model_hub.async_owner import await_owned_task
from core.handlers.model_hub.classification import (
    SOURCE_SETTLEMENT_AUTHORITY,
    classify_outcome,
    source_settlement_allowed,
    terminal_outcome_category,
)
from core.handlers.model_hub.events import (
    BoundedEventLog,
    redact_credential_material,
)
from core.handlers.model_hub.errors import ModelDiscoveryError
from core.handlers.model_hub.provenance import (
    TurnSupplyBlocker,
    produce_turn_outcome,
    project_turn_outcome_copy,
)
from core.handlers.model_hub.request import ModelHubRequest
from core.handlers.model_hub.resolver import (
    allowed_origins,
    inspect_exact_hop,
    resolve_model_hub_turn,
)
from core.handlers.model_hub.revocations import CredentialRevocationJournal
from core.handlers.model_hub.service import (
    ModelHubError,
    ModelHubService,
    _matching_v1_model_id,
)


E64_SETTLEMENT_BOUNDARIES = json.loads(
    (
        Path(__file__).parent
        / "fixtures/model_hub/e64_settlement_boundaries.json"
    ).read_text(encoding="utf-8")
)


class MemoryStore:
    def __init__(self, config: ModelHubConfig):
        self.config = config
        self.requested_models = {"claude": "claude-opus-4-6"}
        self.recovery = False

    def load(self) -> ModelHubConfig:
        return self.config

    def save(self, config: ModelHubConfig) -> None:
        if self.recovery:
            raise ValueError(
                "Config was loaded with recovery warnings; repair the backed-up config before saving changes"
            )
        self.config = config

    def requested_model(self, backend: str) -> str:
        return self.requested_models.get(backend, "")


def test_source_settlement_authority_never_downgrades_a_decided_error() -> None:
    assert source_settlement_allowed("error", "network") is False
    assert source_settlement_allowed("cooldown", "unclassified_error") is True


def test_source_settlement_authority_is_transitive() -> None:
    representatives = {
        rule.status: reason
        for reason, rule in SOURCE_SETTLEMENT_AUTHORITY.items()
    }
    assert set(representatives) == {"cooldown", "error", "needs_action"}
    assert len({rule.priority for rule in SOURCE_SETTLEMENT_AUTHORITY.values()}) == len(
        representatives
    )
    for existing_status in ("active", "standby", *representatives):
        for middle_status, middle_reason in representatives.items():
            for final_reason in representatives.values():
                if source_settlement_allowed(
                    existing_status, middle_reason
                ) and source_settlement_allowed(middle_status, final_reason):
                    assert source_settlement_allowed(existing_status, final_reason)


@pytest.mark.parametrize(
    "fixture",
    E64_SETTLEMENT_BOUNDARIES["stream_errors"],
    ids=lambda fixture: fixture["code"],
)
def test_streamed_native_transient_errors_keep_their_settlement_class(
    fixture: dict[str, object],
) -> None:
    decision = classify_outcome(
        RawCallOutcome(
            kind=RawOutcomeKind.HTTP_ERROR,
            http_status=200,
            error_code=str(fixture["code"]),
            redacted_message=None,
            stream_started=True,
            model_id="upstream-model",
            source_id="src_streamerr01",
        )
    )

    assert decision.action == "surface"
    assert decision.reason == fixture["reason"]
    assert decision.cooldown_seconds == fixture["cooldown_seconds"]
    assert decision.error_code == "stream_interrupted"


class FakeInvokeHandle:
    def __init__(self, outcome: RawCallOutcome):
        self._outcome = outcome
        self.stream = None

    @property
    def observed(self):
        return None

    async def outcome(self) -> RawCallOutcome:
        return self._outcome


class FakeAdapter:
    def __init__(self, discovered: tuple[str, ...] = ("claude-opus-4-6",)):
        self.discovered = discovered
        self.observation: SourceObservation | None = None
        self.observation_error: BaseException | None = None
        self.discovery_error: BaseException | None = None
        self.observed_protocol_orders: list[tuple[str, ...]] = []
        self.revoked: list[str] = []
        self.revoke_error = False
        self.revoke_started: asyncio.Event | None = None
        self.revoke_block: asyncio.Event | None = None
        self.provisioned: list[str] = []
        self.retargeted: list[tuple[str, str | None, str]] = []
        self.synced: list[tuple] = []
        self.discovery_started: asyncio.Event | None = None
        self.discovery_block: asyncio.Event | None = None
        self.provision_started: asyncio.Event | None = None
        self.provision_block: asyncio.Event | None = None
        self.sync_started: asyncio.Event | None = None
        self.sync_block: asyncio.Event | None = None
        self.outcomes = deque()
        self.invocations: list[tuple[str, str]] = []
        self.invocation_requests: list[Mapping[str, object]] = []
        self.refreshable_credential_refs: set[str] = set()
        self.capability_queries: list[str] = []

    async def ensure_installed(self):
        return await self.status()

    async def start(self):
        return await self.status()

    async def stop(self):
        return None

    async def status(self):
        return EngineStatus(EngineHealth.OK, "test", True, "127.0.0.1", 15220, None)

    async def gateway_token(self):
        return "local-test-token"

    async def provision_credential(self, vendor, protocol, secret, base_url):
        self.provisioned.append(secret)
        return f"cred_{len(self.provisioned):08d}"

    async def provision_transient_credential(self, vendor, secret, base_url):
        self.provisioned.append(secret)
        if self.provision_started is not None:
            self.provision_started.set()
        if self.provision_block is not None:
            await self.provision_block.wait()
        return f"cred_{len(self.provisioned):08d}"

    async def retarget_api_key_credential(
        self,
        credential_ref,
        vendor,
        protocol,
        base_url,
    ):
        replacement_ref = f"cred_retarget{len(self.retargeted) + 1:04d}"
        self.retargeted.append((credential_ref, base_url, replacement_ref))
        return replacement_ref

    async def revoke_credential(self, credential_ref):
        if self.revoke_error:
            raise RuntimeError("injected revoke failure")
        if self.revoke_started is not None:
            self.revoke_started.set()
        if self.revoke_block is not None:
            await self.revoke_block.wait()
        self.revoked.append(credential_ref)

    async def credential_supports_refresh(self, credential_ref):
        self.capability_queries.append(credential_ref)
        return credential_ref in self.refreshable_credential_refs

    async def sync_sources(self, bindings):
        self.synced.append(tuple(bindings))
        if self.sync_started is not None:
            self.sync_started.set()
        if self.sync_block is not None:
            await self.sync_block.wait()

    async def discover_models(self, vendor, protocol, base_url, credential_ref):
        if self.discovery_started is not None:
            self.discovery_started.set()
        if self.discovery_block is not None:
            await self.discovery_block.wait()
        if self.discovery_error is not None:
            raise self.discovery_error
        return tuple(DiscoveredModel(id=model_id) for model_id in self.discovered)

    async def observe_source(self, vendor, base_url, credential_ref, protocol_order):
        self.observed_protocol_orders.append(tuple(protocol_order))
        if self.discovery_started is not None:
            self.discovery_started.set()
        if self.discovery_block is not None:
            await self.discovery_block.wait()
        if self.observation_error is not None:
            raise self.observation_error
        return self.observation or SourceObservation(
            outcome=ObservationOutcome.OBSERVED,
            reachable=True,
            authenticated=True,
            protocol=protocol_order[0],
            discovery=ObservationDiscovery.SUCCEEDED,
            models=tuple(DiscoveredModel(id=model_id) for model_id in self.discovered),
        )

    async def invoke(self, source_id, model_id, request, stream, origin):
        self.invocations.append((source_id, model_id))
        self.invocation_requests.append(request)
        outcome = self.outcomes.popleft()
        return FakeInvokeHandle(outcome)


def _source(
    source_id: str,
    model_ids: tuple[str, ...] = ("claude-opus-4-6",),
    *,
    kind: str = "api_key",
    vendor: str = "anthropic",
    channel: str = "hub",
    status: str = "standby",
    credential_ref: str | None = "cred_source",
) -> ModelHubSourceConfig:
    return ModelHubSourceConfig(
        id=source_id,
        kind=kind,
        vendor=vendor,
        display_name=source_id,
        protocol="anthropic" if vendor == "anthropic" else "openai_responses",
        supply_channel=channel,
        billing="monthly" if kind == "subscription" else "metered",
        state=ModelHubSourceStateConfig(status=status),
        models=[ModelHubModelConfig(id=item, provenance="discovered") for item in model_ids],
        credential_ref=credential_ref,
    )


def _config(sources: list[ModelHubSourceConfig], *, model: str = "claude-opus-4-6") -> ModelHubConfig:
    agents = {
        backend: ModelHubAgentSupplyConfig.default(backend, mode="hub")
        for backend in ("claude", "codex", "opencode")
    }
    for backend, agent in agents.items():
        eligible = [source.id for source in sources if ModelHubConfig.source_eligible_for_backend(source, backend)]
        agent.sources.order = eligible
    agents["claude"].routes[model] = ModelHubRouteConfig(
        hops=tuple(ModelHubRouteHopConfig(source.id, model) for source in sources)
    )
    return ModelHubConfig(sources=sources, agents=agents)


def _service(tmp_path: Path, config: ModelHubConfig, adapter: FakeAdapter | None = None) -> tuple[ModelHubService, MemoryStore, FakeAdapter]:
    store = MemoryStore(config)
    adapter = adapter or FakeAdapter()
    service = ModelHubService(
        store=store,
        adapter=adapter,
        events=BoundedEventLog(tmp_path / "events.json"),
        revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
        now=lambda: datetime(2026, 8, 9, tzinfo=timezone.utc),
        requested_model_override=store.requested_model,
    )
    return service, store, adapter


def test_matching_v1_claude_aliases_persist_concrete_observed_id():
    source = _source(
        "src_claudev1",
        (
            "opus",
            "claude-opus-4-6",
            "claude-opus-4-5-20251101",
            "claude-opus-4-6-20260101",
            "claude-opus-4-6-20260115",
        ),
        kind="subscription",
        channel="native_cli",
        credential_ref=None,
    )
    assert _matching_v1_model_id(backend="claude", requested_model="claude-opus-4-6", source=source) == "claude-opus-4-6-20260115"
    assert _matching_v1_model_id(backend="claude", requested_model="opus", source=source) == "claude-opus-4-6-20260115"
    assert _matching_v1_model_id(backend="claude", requested_model="claude-opus-4-6-20260101", source=source) == "claude-opus-4-6-20260101"
    assert _matching_v1_model_id(backend="claude", requested_model="claude-opus-4-6-20250101", source=source) is None


def test_matching_v1_is_literal_for_non_native_and_codex():
    source = _source("src_literal1", ("claude-opus-4-6-20260115",), vendor="anthropic")
    assert _matching_v1_model_id(backend="claude", requested_model="claude-opus-4-6", source=source) is None
    assert _matching_v1_model_id(backend="codex", requested_model="claude-opus-4-6", source=source) is None
    assert _matching_v1_model_id(backend="claude", requested_model="claude-opus-4-6-20260115", source=source) == "claude-opus-4-6-20260115"


def test_matching_v1_opencode_uses_the_same_literal_identity_as_codex():
    source = _source("src_openv001", ("x",), vendor="custom")
    assert _matching_v1_model_id(backend="opencode", requested_model="x", source=source) == "x"
    assert _matching_v1_model_id(backend="opencode", requested_model="custom/x", source=source) is None


def test_matching_v1_never_routes_to_manual_inventory():
    source = _source("src_manual001", ("observed",), vendor="custom")
    source.models.append(ModelHubModelConfig(id="manual-target", provenance="manual"))

    assert (
        _matching_v1_model_id(
            backend="codex",
            requested_model="manual-target",
            source=source,
        )
        is None
    )
    assert (
        _matching_v1_model_id(
            backend="opencode",
            requested_model="manual-target",
            source=source,
        )
        is None
    )


def test_runtime_opencode_resolution_never_repeats_bare_name_matching():
    source = _source("src_openv001", ("x",), vendor="custom")
    config = _config([source], model="x")
    agent = config.agents["opencode"]
    agent.menu = ModelHubMenuConfig(view="featured", checked=["x"])
    agent.models = [ModelHubBackendModelConfig(id="x", native_protocol="openai_responses")]
    agent.routes["x"] = ModelHubRouteConfig(
        hops=(ModelHubRouteHopConfig(source.id, "x"),)
    )

    exact = resolve_model_hub_turn(config, "opencode", "x")
    prefixed = resolve_model_hub_turn(config, "opencode", "custom/x")

    assert exact.source is source
    assert exact.target_model == "x"
    assert prefixed.source is None


def test_runtime_opencode_resolution_preserves_absent_selection():
    source = _source("src_openv002", ("x",), vendor="custom")
    config = _config([source], model="x")
    agent = config.agents["opencode"]
    agent.menu = ModelHubMenuConfig(view="featured", checked=["x"])
    agent.models = [ModelHubBackendModelConfig(id="x", native_protocol="openai_responses")]
    agent.routes["x"] = ModelHubRouteConfig(
        hops=(ModelHubRouteHopConfig(source.id, "x"),)
    )

    resolution = resolve_model_hub_turn(config, "opencode", "")

    assert resolution.requested_model == ""
    assert resolution.source is None


def test_hub_subscription_is_cross_backend_eligible_and_origin_unrestricted():
    source = _source(
        "src_hubsub002",
        ("shared-model",),
        kind="subscription",
        vendor="anthropic",
        channel="hub",
    )
    config = _config([source], model="shared-model")
    config.agents["codex"].routes["shared-model"] = ModelHubRouteConfig(
        hops=(ModelHubRouteHopConfig(source.id, "shared-model"),)
    )
    config.agents["codex"].sources.order = [source.id]

    assert all(ModelHubConfig.source_eligible_for_backend(source, backend) for backend in ("claude", "codex", "opencode"))
    assert allowed_origins(source) == ("claude", "codex", "opencode")
    resolution = resolve_model_hub_turn(config, "codex", "shared-model")
    assert resolution.source is source


def test_runtime_resolution_walks_persisted_route_order_only():
    first = _source("src_route001", ("requested",))
    second = _source("src_route002", ("requested",))
    config = _config([first, second], model="requested")
    config.agents["claude"].sources.order = [second.id, first.id]
    resolution = resolve_model_hub_turn(config, "claude", "requested")
    assert resolution.source is first
    assert resolution.target_model == "requested"
    assert [source.id for source in resolution.matching_sources] == [first.id, second.id]


def test_runtime_fallback_uses_selected_hops_exact_model_id():
    first = _source("src_route004", ("upstream-first",))
    second = _source("src_route005", ("upstream-second",))
    config = _config([first, second], model="requested")
    config.agents["claude"].routes["requested"] = ModelHubRouteConfig(
        hops=(
            ModelHubRouteHopConfig(first.id, "upstream-first"),
            ModelHubRouteHopConfig(second.id, "upstream-second"),
        )
    )

    resolution = resolve_model_hub_turn(
        config,
        "claude",
        "requested",
        unavailable_source_ids=frozenset({first.id}),
    )

    assert resolution.source is second
    assert resolution.target_model == "upstream-second"
    assert resolution.supply_status == "degraded"


def test_supply_is_degraded_when_a_later_exact_hop_is_blocked():
    first = _source("src_route020", ("requested",))
    second = _source("src_route021", ("requested",), status="cooldown")
    second.state = ModelHubSourceStateConfig(
        status="cooldown",
        retry_at="2099-01-01T00:00:00Z",
        detail_key="models.source.cooldown.rate_limited",
    )
    config = _config([first, second], model="requested")

    resolution = resolve_model_hub_turn(config, "claude", "requested")

    assert resolution.source is first
    assert resolution.supply_status == "degraded"


def test_streamed_protocol_failure_preserves_its_positive_terminal_category():
    decision = classify_outcome(
        RawCallOutcome(
            kind=RawOutcomeKind.PROTOCOL_ERROR,
            http_status=502,
            error_code="upstream_protocol_error",
            redacted_message=None,
            stream_started=True,
            model_id="upstream-first",
            source_id="src_route006",
        )
    )

    assert decision.action == "surface"
    assert decision.reason is None
    assert decision.error_code == "upstream_protocol_error"


@pytest.mark.parametrize(
    ("outcome", "expected_category"),
    [
        (
            RawCallOutcome(
                kind=RawOutcomeKind.SUCCESS,
                http_status=200,
                error_code=None,
                redacted_message=None,
                stream_started=True,
                model_id="upstream-first",
                source_id="src_route006",
            ),
            "served",
        ),
        (
            RawCallOutcome(
                kind=RawOutcomeKind.HTTP_ERROR,
                http_status=403,
                error_code="permission_error",
                redacted_message=None,
                stream_started=True,
                model_id="upstream-first",
                source_id="src_route006",
            ),
            "request_nonfallback",
        ),
        (
            RawCallOutcome(
                kind=RawOutcomeKind.PROTOCOL_ERROR,
                http_status=502,
                error_code=None,
                redacted_message=None,
                stream_started=True,
                model_id="upstream-first",
                source_id="src_route006",
            ),
            "upstream_protocol",
        ),
        (
            RawCallOutcome(
                kind=RawOutcomeKind.NETWORK_ERROR,
                http_status=200,
                error_code="engine_down",
                redacted_message="loopback transport failed",
                stream_started=True,
                model_id="upstream-first",
                source_id="src_route006",
            ),
            "engine_down",
        ),
        (
            RawCallOutcome(
                kind=RawOutcomeKind.NETWORK_ERROR,
                http_status=None,
                error_code=None,
                redacted_message=None,
                stream_started=True,
                model_id="upstream-first",
                source_id="src_route006",
            ),
            "fallback_source",
        ),
    ],
)
def test_terminal_outcome_category_authority_has_a_behavior_consumer(
    outcome: RawCallOutcome,
    expected_category: str,
) -> None:
    assert terminal_outcome_category(
        outcome,
        classify_outcome(outcome),
    ) == expected_category


@pytest.mark.parametrize("status", [401, 402, 403, 429, 500])
def test_machine_permission_denial_precedes_every_status_heuristic(
    tmp_path,
    status,
):
    source = _source("src_route006", ("upstream-first", "upstream-second"))
    config = _config([source])
    config.agents["claude"].routes["claude-opus-4-6"] = ModelHubRouteConfig(
        hops=(
            ModelHubRouteHopConfig(source.id, "upstream-first"),
            ModelHubRouteHopConfig(source.id, "upstream-second"),
        )
    )
    adapter = FakeAdapter()
    adapter.outcomes.extend(
        (
            RawCallOutcome(
                kind=RawOutcomeKind.HTTP_ERROR,
                http_status=status,
                error_code="permission_error",
                redacted_message=None,
                stream_started=False,
                model_id="upstream-first",
                source_id=source.id,
            ),
            RawCallOutcome(
                kind=RawOutcomeKind.SUCCESS,
                http_status=200,
                error_code=None,
                redacted_message=None,
                stream_started=False,
                model_id="upstream-second",
                source_id=source.id,
            ),
        )
    )
    service, store, _ = _service(tmp_path, config, adapter)

    decisions = []
    with pytest.raises(ModelHubError) as exc:
        asyncio.run(
            service.resolve(
                backend="claude",
                model_id="claude-opus-4-6",
                request={},
                attempt_observer=lambda *args: decisions.append(args[5]),
            )
        )

    assert exc.value.code == "request_incompatible"
    assert exc.value.status == 403
    assert decisions[-1].action == "surface"
    assert adapter.invocations == [(source.id, "upstream-first")]
    assert adapter.capability_queries == []
    assert store.load().sources[0].state.status == "standby"
    assert BoundedEventLog(tmp_path / "events.json").list() == []


def test_runtime_skips_later_hops_after_a_source_global_failure(tmp_path):
    first = _source("src_route022", ("upstream-first", "upstream-second"))
    second = _source("src_route023", ("upstream-third",))
    config = _config([first, second])
    config.agents["claude"].routes["claude-opus-4-6"] = ModelHubRouteConfig(
        hops=(
            ModelHubRouteHopConfig(first.id, "upstream-first"),
            ModelHubRouteHopConfig(first.id, "upstream-second"),
            ModelHubRouteHopConfig(second.id, "upstream-third"),
        )
    )
    adapter = FakeAdapter()
    adapter.outcomes.extend(
        (
            RawCallOutcome(
                kind=RawOutcomeKind.HTTP_ERROR,
                http_status=429,
                error_code="rate_limit_error",
                redacted_message=None,
                stream_started=False,
                model_id="upstream-first",
                source_id=first.id,
            ),
            RawCallOutcome(
                kind=RawOutcomeKind.SUCCESS,
                http_status=200,
                error_code=None,
                redacted_message=None,
                stream_started=False,
                model_id="upstream-third",
                source_id=second.id,
            ),
        )
    )
    service, _store, _ = _service(tmp_path, config, adapter)

    resolved = asyncio.run(
        service.resolve(
            backend="claude",
            model_id="claude-opus-4-6",
            request={},
        )
    )

    assert adapter.invocations == [
        (first.id, "upstream-first"),
        (second.id, "upstream-third"),
    ]
    assert resolved.model_id == "upstream-third"


def test_runtime_fallback_continues_when_recovery_blocks_runtime_state_save(tmp_path):
    first = _source("src_recovery001", ("upstream-first",))
    second = _source("src_recovery002", ("upstream-second",))
    config = _config([first, second])
    config.agents["claude"].routes["claude-opus-4-6"] = ModelHubRouteConfig(
        hops=(
            ModelHubRouteHopConfig(first.id, "upstream-first"),
            ModelHubRouteHopConfig(second.id, "upstream-second"),
        )
    )
    adapter = FakeAdapter()
    adapter.outcomes.extend(
        (
            RawCallOutcome(
                kind=RawOutcomeKind.HTTP_ERROR,
                http_status=429,
                error_code="rate_limit_error",
                redacted_message=None,
                stream_started=False,
                model_id="upstream-first",
                source_id=first.id,
            ),
            RawCallOutcome(
                kind=RawOutcomeKind.SUCCESS,
                http_status=200,
                error_code=None,
                redacted_message=None,
                stream_started=False,
                model_id="upstream-second",
                source_id=second.id,
            ),
        )
    )
    service, store, _ = _service(tmp_path, config, adapter)
    store.recovery = True

    resolved = asyncio.run(
        service.resolve(
            backend="claude",
            model_id="claude-opus-4-6",
            request={},
        )
    )

    assert adapter.invocations == [
        (first.id, "upstream-first"),
        (second.id, "upstream-second"),
    ]
    assert resolved.source_id == second.id


def test_static_api_key_401_falls_through_without_retry(tmp_path):
    first = _source(
        "src_static401a",
        credential_ref="cred_static401",
    )
    second = _source(
        "src_static401b",
        credential_ref="cred_fallback401",
    )
    config = _config([first, second])
    adapter = FakeAdapter()
    adapter.outcomes.extend(
        (
            RawCallOutcome(
                kind=RawOutcomeKind.HTTP_ERROR,
                http_status=401,
                error_code="unauthorized",
                redacted_message=None,
                stream_started=False,
                model_id="claude-opus-4-6",
                source_id=first.id,
            ),
            RawCallOutcome(
                kind=RawOutcomeKind.SUCCESS,
                http_status=200,
                error_code=None,
                redacted_message=None,
                stream_started=False,
                model_id="claude-opus-4-6",
                source_id=second.id,
            ),
        )
    )
    service, store, _ = _service(tmp_path, config, adapter)

    resolved = asyncio.run(
        service.resolve(
            backend="claude",
            model_id="claude-opus-4-6",
            request={},
        )
    )

    assert adapter.invocations == [
        (first.id, "claude-opus-4-6"),
        (second.id, "claude-opus-4-6"),
    ]
    assert adapter.capability_queries == ["cred_static401"]
    assert resolved.source_id == second.id
    persisted = next(item for item in store.load().sources if item.id == first.id)
    assert persisted.state.status == "needs_action"
    assert persisted.state.detail_key == (
        "models.source.needs_action.credential_revoked"
    )


def test_401_retry_depends_on_credential_capability_not_source_kind(tmp_path):
    source = _source(
        "src_refresh401",
        credential_ref="cred_refresh401",
    )
    config = _config([source])
    adapter = FakeAdapter()
    adapter.refreshable_credential_refs.add("cred_refresh401")
    adapter.outcomes.extend(
        (
            RawCallOutcome(
                kind=RawOutcomeKind.HTTP_ERROR,
                http_status=401,
                error_code="unauthorized",
                redacted_message=None,
                stream_started=False,
                model_id="claude-opus-4-6",
                source_id=source.id,
            ),
            RawCallOutcome(
                kind=RawOutcomeKind.SUCCESS,
                http_status=200,
                error_code=None,
                redacted_message=None,
                stream_started=False,
                model_id="claude-opus-4-6",
                source_id=source.id,
            ),
        )
    )
    service, store, _ = _service(tmp_path, config, adapter)

    resolved = asyncio.run(
        service.resolve(
            backend="claude",
            model_id="claude-opus-4-6",
            request={},
        )
    )

    assert adapter.invocations == [
        (source.id, "claude-opus-4-6"),
        (source.id, "claude-opus-4-6"),
    ]
    assert adapter.capability_queries == ["cred_refresh401"]
    assert resolved.source_id == source.id
    assert store.load().sources[0].state.status == "standby"


def test_runtime_filters_reasoning_effort_for_each_exact_hop(tmp_path):
    first = _source("src_effort001", ("upstream-first",))
    second = _source("src_effort002", ("upstream-second",))
    first.models[0].reasoning_efforts = ["high"]
    second.models[0].reasoning_efforts = ["low"]
    config = _config([first, second])
    config.agents["claude"].routes["claude-opus-4-6"] = ModelHubRouteConfig(
        hops=(
            ModelHubRouteHopConfig(first.id, "upstream-first"),
            ModelHubRouteHopConfig(second.id, "upstream-second"),
        )
    )
    adapter = FakeAdapter()
    adapter.outcomes.extend(
        (
            RawCallOutcome(
                kind=RawOutcomeKind.HTTP_ERROR,
                http_status=429,
                error_code="rate_limit_error",
                redacted_message=None,
                stream_started=False,
                model_id="upstream-first",
                source_id=first.id,
            ),
            RawCallOutcome(
                kind=RawOutcomeKind.SUCCESS,
                http_status=200,
                error_code=None,
                redacted_message=None,
                stream_started=False,
                model_id="upstream-second",
                source_id=second.id,
            ),
        )
    )
    service, _store, _ = _service(tmp_path, config, adapter)
    request = ModelHubRequest(
        {"reasoning": {"effort": "high", "summary": "auto"}},
        protocol="openai_responses",
        headers={"x-test": "preserved"},
    )
    observed_attempts = []

    resolved = asyncio.run(
        service.resolve(
            backend="claude",
            model_id="claude-opus-4-6",
            request=request,
            attempt_observer=lambda *args: observed_attempts.append(args),
        )
    )

    assert resolved.source_id == second.id
    assert adapter.invocation_requests[0]["reasoning"] == {
        "effort": "high",
        "summary": "auto",
    }
    assert adapter.invocation_requests[1]["reasoning"] == {"summary": "auto"}
    assert adapter.invocation_requests[1].protocol == "openai_responses"
    assert adapter.invocation_requests[1].headers == {"x-test": "preserved"}
    started = [attempt for attempt in observed_attempts if attempt[4] is None]
    assert [attempt[0] for attempt in started] == [first.id, second.id]
    assert started[0][6:] == ((), ())
    assert started[1][6:] == (("high",), ("low",))


def test_runtime_omits_unsupported_direct_reasoning_effort(tmp_path):
    source = _source("src_effort003", ("upstream-model",))
    config = _config([source])
    config.agents["claude"].routes["claude-opus-4-6"] = ModelHubRouteConfig(
        hops=(ModelHubRouteHopConfig(source.id, "upstream-model"),)
    )
    adapter = FakeAdapter()
    adapter.outcomes.append(
        RawCallOutcome(
            kind=RawOutcomeKind.SUCCESS,
            http_status=200,
            error_code=None,
            redacted_message=None,
            stream_started=False,
            model_id="upstream-model",
            source_id=source.id,
        )
    )
    service, _store, _ = _service(tmp_path, config, adapter)
    request = ModelHubRequest(
        {"reasoning_effort": "high"},
        protocol="openai_chat",
        headers={"x-test": "preserved"},
    )

    resolved = asyncio.run(
        service.resolve(
            backend="claude",
            model_id="claude-opus-4-6",
            request=request,
        )
    )

    assert resolved.source_id == source.id
    assert "reasoning_effort" not in adapter.invocation_requests[0]
    assert adapter.invocation_requests[0].protocol == "openai_chat"
    assert adapter.invocation_requests[0].headers == {"x-test": "preserved"}


def test_reasoning_effort_strip_log_redacts_values_and_names_declared_tiers(
    tmp_path,
    caplog,
):
    source = _source("src_effortlog1", ("upstream-model",))
    source.models[0].reasoning_efforts = ["low", "high"]
    config = _config([source])
    config.agents["claude"].routes["claude-opus-4-6"] = ModelHubRouteConfig(
        hops=(ModelHubRouteHopConfig(source.id, "upstream-model"),)
    )
    adapter = FakeAdapter()
    adapter.outcomes.append(
        RawCallOutcome(
            kind=RawOutcomeKind.SUCCESS,
            http_status=200,
            error_code=None,
            redacted_message=None,
            stream_started=False,
            model_id="upstream-model",
            source_id=source.id,
        )
    )
    service, _store, _ = _service(tmp_path, config, adapter)

    with caplog.at_level("INFO", logger="core.handlers.model_hub.service"):
        asyncio.run(
            service.resolve(
                backend="claude",
                model_id="claude-opus-4-6",
                request={
                    "reasoning_effort": (
                        "authorization: sk-test-strip-secret-material"
                    )
                },
            )
        )

    assert "sk-test-strip-secret-material" not in caplog.text
    assert "[redacted]" in caplog.text
    assert "declared tiers: ['low', 'high']" in caplog.text


def test_reasoning_effort_telemetry_is_utf8_bounded_after_redaction(caplog):
    source = _source("src_effortbound", ("upstream-model",))
    long_declared = "层级" * 200
    source.models[0].reasoning_efforts = ["low", long_declared]
    long_stripped = (
        "深度" * 200 + " authorization: sk-test-strip-secret-material"
    )

    with caplog.at_level("INFO", logger="core.handlers.model_hub.service"):
        result = ModelHubService._request_for_exact_reasoning_effort(
            {"reasoning_effort": long_stripped},
            source,
            "upstream-model",
        )

    [stripped] = result.stripped_efforts
    [declared] = result.declared_efforts
    safe_stripped = redact_credential_material(long_stripped)
    stripped_digest = hashlib.sha256(safe_stripped.encode("utf-8")).hexdigest()
    declared_payload = json.dumps(
        ("low", long_declared),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    declared_digest = hashlib.sha256(declared_payload.encode("utf-8")).hexdigest()
    assert len(stripped.encode("utf-8")) <= 256
    assert len(declared.encode("utf-8")) <= 256
    assert stripped.endswith(f"[sha256:{stripped_digest}]")
    assert declared.endswith(f"[sha256:{declared_digest}]")
    assert hashlib.sha256(long_stripped.encode("utf-8")).hexdigest() not in stripped
    assert long_stripped not in caplog.text
    assert long_declared not in caplog.text
    assert "sk-test-strip-secret-material" not in caplog.text
    assert stripped in caplog.text
    assert declared in caplog.text


def test_reasoning_effort_telemetry_bounds_many_short_declared_tiers():
    source = _source("src_effortmany", ("upstream-model",))
    source.models[0].reasoning_efforts = [
        f"tier-{index:04d}" for index in range(1_000)
    ]

    result = ModelHubService._request_for_exact_reasoning_effort(
        {"reasoning_effort": "undeclared"},
        source,
        "upstream-model",
    )

    [declared] = result.declared_efforts
    assert len(declared.encode("utf-8")) <= 256
    assert declared.endswith("]")
    assert "[sha256:" in declared


def test_reasoning_effort_telemetry_preserves_short_values_exactly():
    source = _source("src_effortshort", ("upstream-model",))
    source.models[0].reasoning_efforts = ["low", "high"]

    result = ModelHubService._request_for_exact_reasoning_effort(
        {"reasoning_effort": "ultra"},
        source,
        "upstream-model",
    )

    assert result.stripped_efforts == ("ultra",)
    assert result.declared_efforts == ("low", "high")


@pytest.mark.parametrize(
    ("request_payload", "expected_request", "expected_stripped"),
    [
        ({"reasoning_effort": ""}, {}, ("",)),
        ({"reasoning_effort": 7}, {}, ("<int>",)),
        (
            {"reasoning": {"effort": ["high"], "summary": "auto"}},
            {"reasoning": {"summary": "auto"}},
            ("<list>",),
        ),
        (
            {"reasoning": {"effort": None}},
            {},
            ("<null>",),
        ),
    ],
    ids=("direct-empty", "direct-int", "nested-list", "nested-null"),
)
def test_reasoning_effort_telemetry_records_every_stripped_value(
    request_payload,
    expected_request,
    expected_stripped,
):
    source = _source("src_efforttype", ("upstream-model",))
    source.models[0].reasoning_efforts = ["high"]

    result = ModelHubService._request_for_exact_reasoning_effort(
        request_payload,
        source,
        "upstream-model",
    )

    assert result.request == expected_request
    assert result.stripped_efforts == expected_stripped
    assert result.declared_efforts == ("high",)


def test_runtime_filters_reasoning_effort_forms_independently(tmp_path):
    source = _source("src_effort004", ("upstream-model",))
    source.models[0].reasoning_efforts = ["high"]
    config = _config([source])
    config.agents["claude"].routes["claude-opus-4-6"] = ModelHubRouteConfig(
        hops=(ModelHubRouteHopConfig(source.id, "upstream-model"),)
    )
    adapter = FakeAdapter()
    adapter.outcomes.append(
        RawCallOutcome(
            kind=RawOutcomeKind.SUCCESS,
            http_status=200,
            error_code=None,
            redacted_message=None,
            stream_started=False,
            model_id="upstream-model",
            source_id=source.id,
        )
    )
    service, _store, _ = _service(tmp_path, config, adapter)
    request = ModelHubRequest(
        {
            "reasoning_effort": "high",
            "reasoning": {"effort": "ultra", "summary": "auto"},
        },
        protocol="openai_responses",
        headers={},
    )

    resolved = asyncio.run(
        service.resolve(
            backend="claude",
            model_id="claude-opus-4-6",
            request=request,
        )
    )

    assert resolved.source_id == source.id
    assert adapter.invocation_requests[0]["reasoning_effort"] == "high"
    assert adapter.invocation_requests[0]["reasoning"] == {"summary": "auto"}


def test_runtime_does_not_alias_unpersisted_claude_request():
    source = _source("src_route003", ("claude-opus-4-6-20260115",))
    config = _config([source], model="claude-opus-4-6-20260115")
    resolution = resolve_model_hub_turn(config, "claude", "claude-opus-4-6")
    assert resolution.source is None
    assert resolution.supply_status == "interrupted"


def test_resolution_marks_stale_exact_hop_unsupported():
    source = _source("src_stale001", ("other-model",))
    config = _config([source], model="stale-model")
    resolution = resolve_model_hub_turn(config, "claude", "stale-model")
    assert resolution.matching_sources == (source,)
    assert resolution.candidates == ()
    assert resolution.unsupported_source_ids == (source.id,)
    assert resolution.projectable_hops == resolution.inspected_hops


def test_launch_failure_reports_unsupported_exact_hop():
    source = _source("src_launch001", ("other-model",))
    config = _config([source], model="stale-model")
    resolution = resolve_model_hub_turn(config, "claude", "stale-model")

    projection = produce_turn_outcome(
        "turn.no_candidate.blocked",
        config=config,
        resolution=resolution,
    )
    facts = projection.supply_facts
    assert facts is not None
    copy = project_turn_outcome_copy(projection)

    assert copy is not None
    assert copy.key == "modelHub.launch.interrupted"
    assert facts.blockers == (
        TurnSupplyBlocker(source.display_name, "model_unsupported"),
    )


def test_exact_hop_inspection_is_the_single_identity_and_supply_authority():
    source = _source("src_exact001", ("upstream-model",), vendor="openai")
    config = _config([source], model="menu-model")
    hop = ModelHubRouteHopConfig(source.id, "upstream-model")

    inspected = inspect_exact_hop(config, "opencode", "menu-model", hop)

    assert inspected.identity == (
        "opencode",
        "menu-model",
        source.id,
        "upstream-model",
    )
    assert inspected.configuration_eligible is True
    assert inspected.inventory_member is True
    assert inspected.supply_eligible is True
    assert inspected.runnable is True
    assert inspected.reason is None


def test_exact_hop_inspection_rejects_wrong_identity_and_empty_route():
    source = _source("src_exact002", ("actual-model",), vendor="openai")
    config = _config([source], model="menu-model")

    wrong = inspect_exact_hop(
        config,
        "opencode",
        "menu-model",
        ModelHubRouteHopConfig(source.id, "wrong-model"),
    )
    empty = inspect_exact_hop(config, "opencode", "menu-model", None)

    assert wrong.inventory_member is False
    assert wrong.supply_eligible is False
    assert wrong.runnable is False
    assert wrong.reason == "model_unsupported"
    assert empty.identity == ("opencode", "menu-model", None, None)
    assert empty.reason == "route_unconfigured"


def test_explicit_opencode_selection_outside_menu_remains_visible(tmp_path):
    config = ModelHubConfig()
    config.agents["opencode"].mode = "hub"
    config.agents["opencode"].menu.checked = ["visible-model"]
    config.agents["opencode"].models = [
        ModelHubBackendModelConfig(id="visible-model", native_protocol="openai_responses")
    ]
    service, store, _ = _service(tmp_path, config)
    store.requested_models["opencode"] = "hidden-model"
    service.named_agents_override = lambda backend: (
        [("researcher", "hidden-model")] if backend == "opencode" else []
    )

    payload = service.get_agent_sources("opencode")
    resolution = resolve_model_hub_turn(
        config,
        "opencode",
        "hidden-model",
    )

    assert payload["selected_model_id"] == "hidden-model"
    assert payload["supply_status"] == "interrupted"
    assert payload["named_agents"] == [
        {
            "name": "researcher",
            "effective_model_id": "hidden-model",
            "supply_status": "interrupted",
            "route_reason": "route_unconfigured",
        }
    ]
    assert resolution.requested_model == "hidden-model"
    assert resolution.route_unconfigured is True
    assert resolution.route_reason == "route_unconfigured"


def test_direct_mode_rejects_chain_write_before_config_mutation(tmp_path):
    menu_model = "claude-opus-4-6"
    source = _source("src_direct01", (menu_model,))
    config = _config([source], model=menu_model)
    service, store, _ = _service(tmp_path, config)
    asyncio.run(service.set_agent_mode("claude", "direct"))
    before = store.load().to_payload()

    with pytest.raises(ModelHubError) as exc:
        asyncio.run(
            service.set_agent_chain(
                "claude",
                menu_model,
                {"hops": []},
            )
        )

    assert exc.value.code == "direct_mode"
    assert store.load().to_payload() == before


@pytest.mark.parametrize(
    ("backend", "hidden_model"),
    [
        ("claude", "claude-hidden-model"),
        ("opencode", "hidden-model"),
    ],
)
def test_chain_write_rejects_models_outside_the_current_menu(
    tmp_path,
    backend,
    hidden_model,
):
    source = _source("src_menu0001", ("upstream-model",), vendor="openai")
    config = _config([source])
    config.agents["opencode"].menu.checked = ["visible-model"]
    config.agents["opencode"].models = [
        ModelHubBackendModelConfig(id="visible-model", native_protocol="openai_responses")
    ]
    config.agents["opencode"].routes["visible-model"] = ModelHubRouteConfig()
    service, store, _ = _service(tmp_path, config)
    before = store.load().to_payload()

    with pytest.raises(ModelHubError) as exc:
        asyncio.run(
            service.set_agent_chain(
                backend,
                hidden_model,
                {
                    "hops": [
                        {
                            "source_id": source.id,
                            "model_id": "upstream-model",
                        }
                    ]
                },
            )
        )

    assert exc.value.code == "mapping_target_unavailable"
    assert store.load().to_payload() == before


def test_refresh_ignores_preexisting_unrelated_interruption(tmp_path):
    menu_model = "claude-opus-4-6"
    source = _source("src_refresh02", (menu_model,))
    broken = _source("src_refresh03", ("other",), status="cooldown")
    broken.state = ModelHubSourceStateConfig(
        status="cooldown",
        retry_at="2099-01-01T00:00:00Z",
        detail_key="models.source.cooldown.rate_limited",
    )
    config = _config([source, broken], model=menu_model)
    config.agents["claude"].routes["claude-sonnet-4-6"] = ModelHubRouteConfig(
        hops=(ModelHubRouteHopConfig(broken.id, "other"),)
    )
    adapter = FakeAdapter(discovered=(menu_model,))
    service, _store, _ = _service(tmp_path, config, adapter)

    result = asyncio.run(service.refresh_source(source.id))

    assert result["removed_hops"] == []
    assert result["interrupted"] == []


def test_engine_binding_excludes_empty_inventory(tmp_path):
    source = _source("src_empty01", (), vendor="openai")
    config = _config([source], model="requested")
    service, _store, _ = _service(tmp_path, config)

    assert service._bindings(config) == []


def test_agent_chain_projects_exact_hops_and_blockers(tmp_path):
    source = _source("src_chain001", ("other-model",))
    config = _config([source], model="stale-model")
    service, _, _ = _service(tmp_path, config)
    payload = service.agent_chain("claude", "stale-model")
    assert payload["chain"] == [
        {
            "source_id": source.id,
            "model_id": "stale-model",
            "channel": "hub",
            "health": "healthy",
            "runnable": False,
            "reason": "model_unsupported",
            "retry_at": None,
        }
    ]
    assert payload["current"] is None
    assert payload["supply_state"] == "interrupted"


def test_set_agent_chain_returns_guarded_exact_route(tmp_path):
    menu_model = "claude-opus-4-6"
    first = _source("src_chain002", (menu_model,))
    second = _source("src_chain003", (menu_model,))
    config = _config([first, second], model=menu_model)
    service, store, _ = _service(tmp_path, config)
    result = asyncio.run(
        service.set_agent_chain(
            "claude",
            menu_model,
            {
                "hops": [
                    {"source_id": second.id, "model_id": menu_model},
                    {"source_id": first.id, "model_id": menu_model},
                ],
                "force": True,
            },
        )
    )
    assert result["removed_hops"] == []
    assert isinstance(result["interrupted"], list)
    assert [item["source_id"] for item in result["chain"]["chain"]] == [second.id, first.id]
    assert result["chain"]["current"] == {"source_id": second.id, "model_id": menu_model}
    assert [hop.source_id for hop in store.load().agents["claude"].routes[menu_model].hops] == [second.id, first.id]


def test_set_agent_chain_reports_complete_removed_hops_without_syncing(tmp_path):
    menu_model = "claude-opus-4-6"
    first = _source("src_chain006", (menu_model,))
    second = _source("src_chain007", (menu_model,))
    config = _config([first, second], model=menu_model)
    service, _store, adapter = _service(tmp_path, config)

    result = asyncio.run(
        service.set_agent_chain(
            "claude",
            menu_model,
            {
                "hops": [{"source_id": first.id, "model_id": menu_model}],
                "force": True,
            },
        )
    )

    assert result["removed_hops"] == [
        {
            "backend": "claude",
            "menu_model": menu_model,
            "source_id": second.id,
            "model_id": menu_model,
            "position": 2,
        }
    ]
    assert adapter.synced == []


def test_set_agent_chain_ignores_unrelated_existing_gap(tmp_path):
    menu_model = "claude-opus-4-6"
    first = _source("src_chain008", (menu_model,))
    second = _source("src_chain009", (menu_model,))
    broken = _source(
        "src_chain010",
        ("other",),
        status="cooldown",
    )
    broken.state = ModelHubSourceStateConfig(
        status="cooldown",
        retry_at="2099-01-01T00:00:00Z",
        detail_key="models.source.cooldown.rate_limited",
    )
    config = _config([first, second, broken], model=menu_model)
    config.agents["claude"].routes["claude-sonnet-4-6"] = ModelHubRouteConfig(
        hops=(ModelHubRouteHopConfig(broken.id, "other"),)
    )
    service, store, adapter = _service(tmp_path, config)

    result = asyncio.run(
        service.set_agent_chain(
            "claude",
            menu_model,
            {
                "hops": [
                    {"source_id": second.id, "model_id": menu_model},
                    {"source_id": first.id, "model_id": menu_model},
                ]
            },
        )
    )

    assert result["interrupted"] == []
    assert adapter.synced == []
    assert [hop.source_id for hop in store.load().agents["claude"].routes[menu_model].hops] == [second.id, first.id]


def test_source_creation_cancellation_revokes_unsaved_credential(tmp_path):
    adapter = FakeAdapter()
    adapter.discovery_started = asyncio.Event()
    adapter.discovery_block = asyncio.Event()
    service, store, _ = _service(tmp_path, ModelHubConfig(), adapter)

    async def scenario():
        task = asyncio.create_task(
            service.create_source(
                {
                    "kind": "api_key",
                    "vendor": "anthropic",
                    "display_name": "Key",
                    "key": "sk-test-cancel-credential",
                }
            )
        )
        await adapter.discovery_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert len(store.load().sources) == 0
    assert adapter.revoked == ["cred_00000001"]


def test_source_creation_cancellation_during_provision_revokes_owned_credential(tmp_path):
    adapter = FakeAdapter()
    adapter.provision_started = asyncio.Event()
    adapter.provision_block = asyncio.Event()
    service, store, _ = _service(tmp_path, ModelHubConfig(), adapter)

    async def scenario():
        task = asyncio.create_task(
            service.create_source(
                {
                    "kind": "api_key",
                    "vendor": "anthropic",
                    "display_name": "Key",
                    "key": "sk-test-provision-cancel",
                }
            )
        )
        await adapter.provision_started.wait()
        task.cancel()
        adapter.provision_block.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert store.load().sources == []
    assert adapter.revoked == ["cred_00000001"]
    assert service.revocations.list() == []


def test_cancelled_owned_task_is_terminal_without_settlement_retry() -> None:
    async def scenario() -> None:
        async def cancelled_operation() -> None:
            raise asyncio.CancelledError

        task = asyncio.create_task(cancelled_operation())
        await asyncio.sleep(0)
        assert task.cancelled()
        with patch("core.handlers.model_hub.service.asyncio.shield") as shield:
            with pytest.raises(asyncio.CancelledError):
                await await_owned_task(task)
        shield.assert_not_called()

    asyncio.run(scenario())


def test_source_creation_cancellation_after_persist_keeps_source_and_credential(tmp_path):
    adapter = FakeAdapter()
    adapter.sync_started = asyncio.Event()
    adapter.sync_block = asyncio.Event()
    service, store, _ = _service(tmp_path, ModelHubConfig(), adapter)

    async def scenario():
        task = asyncio.create_task(
            service.create_source(
                {
                    "kind": "api_key",
                    "vendor": "anthropic",
                    "display_name": "Key",
                    "key": "sk-test-late-cancel",
                }
            )
        )
        await adapter.sync_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert len(store.load().sources) == 1
    assert store.load().sources[0].credential_ref == "cred_00000002"
    assert adapter.revoked == ["cred_00000001"]


def test_create_source_persists_only_the_response_proven_protocol(tmp_path):
    adapter = FakeAdapter(discovered=("gpt-5.6",))
    service, store, _ = _service(tmp_path, ModelHubConfig(), adapter)

    result = asyncio.run(
        service.create_source(
            {
                "kind": "api_key",
                "vendor": "openai",
                "display_name": "Observed key",
                "key": "sk-test-response-proof",
                "protocol": "openai_responses",
            }
        )
    )

    assert adapter.observed_protocol_orders == [("openai_responses",)]
    assert result["source"]["protocol"] == "openai_responses"
    assert store.load().sources[0].protocol == "openai_responses"
    assert adapter.revoked == ["cred_00000001"]


def test_create_source_normalizes_vendor_before_matching_v1_placement(tmp_path):
    config = ModelHubConfig()
    opencode = config.agents["opencode"]
    opencode.mode = "hub"
    assert opencode.menu is not None
    opencode.menu.checked = ["gpt-5.6"]
    opencode.models = [
        ModelHubBackendModelConfig(
            id="gpt-5.6",
            native_protocol="openai_responses",
        )
    ]
    opencode.routes["gpt-5.6"] = ModelHubRouteConfig()
    adapter = FakeAdapter(discovered=("gpt-5.6",))
    service, store, _ = _service(tmp_path, config, adapter)

    result = asyncio.run(
        service.create_source(
            {
                "kind": "api_key",
                "vendor": "OpenAI",
                "display_name": "Observed key",
                "key": "sk-test-vendor-normalization",
                "protocol": "openai_responses",
            }
        )
    )

    assert result["source"]["vendor"] == "openai"
    assert store.load().sources[0].vendor == "openai"
    assert any(
        position["backend"] == "opencode"
        and position["menu_model"] == "gpt-5.6"
        and position["model_id"] == "gpt-5.6"
        for position in result["added_to"]
    )


def test_ambiguous_observation_never_creates_a_source(tmp_path):
    adapter = FakeAdapter()
    adapter.observation = SourceObservation(
        outcome=ObservationOutcome.AMBIGUOUS,
        reachable=True,
        authenticated=True,
        protocol=None,
        discovery=ObservationDiscovery.NOT_ATTEMPTED,
        models=(),
    )
    service, store, _ = _service(tmp_path, ModelHubConfig(), adapter)

    with pytest.raises(ModelHubError) as exc:
        asyncio.run(
            service.create_source(
                {
                    "kind": "api_key",
                    "vendor": "custom",
                    "display_name": "Ambiguous key",
                    "key": "sk-test-ambiguous",
                }
            )
        )

    assert exc.value.data["observation"]["outcome"] == "ambiguous"
    assert store.load().sources == []
    assert adapter.revoked == ["cred_00000001"]


def test_authentication_failure_observation_never_creates_a_source(tmp_path):
    adapter = FakeAdapter()
    adapter.observation = SourceObservation(
        outcome=ObservationOutcome.AUTHENTICATION_FAILED,
        reachable=True,
        authenticated=False,
        protocol=None,
        discovery=ObservationDiscovery.NOT_ATTEMPTED,
        models=(),
    )
    service, store, _ = _service(tmp_path, ModelHubConfig(), adapter)

    with pytest.raises(ModelHubError) as exc:
        asyncio.run(
            service.create_source(
                {
                    "kind": "api_key",
                    "vendor": "anthropic",
                    "display_name": "Rejected key",
                    "key": "sk-test-auth-rejected",
                }
            )
        )

    assert exc.value.data["observation"]["outcome"] == "authentication_failed"
    assert store.load().sources == []
    assert adapter.revoked == ["cred_00000001"]


def test_create_failure_preserves_human_safe_observation_detail(tmp_path):
    adapter = FakeAdapter()
    adapter.observation = SourceObservation(
        outcome=ObservationOutcome.AUTHENTICATION_FAILED,
        reachable=True,
        authenticated=False,
        protocol=None,
        discovery=ObservationDiscovery.NOT_ATTEMPTED,
        models=(),
    )
    service, store, _ = _service(tmp_path, ModelHubConfig(), adapter)

    with pytest.raises(ModelHubError) as exc:
        asyncio.run(service.create_source({"kind": "api_key", "vendor": "custom", "key": "sk-test-detail"}))

    assert exc.value.code == "discovery_failed"
    assert exc.value.detail == "modelHub.errors.authentication_failed"
    assert exc.value.data["observation"]["outcome"] == "authentication_failed"
    assert store.load().sources == []


@pytest.mark.parametrize(
    ("adapter_result", "expected_outcome"),
    [
        (
            SourceObservation(
                outcome=ObservationOutcome.AUTHENTICATION_FAILED,
                reachable=True,
                authenticated=False,
                protocol=None,
                discovery=ObservationDiscovery.NOT_ATTEMPTED,
                models=(),
            ),
            ObservationOutcome.AUTHENTICATION_FAILED,
        ),
        (RuntimeError("injected adapter failure"), ObservationOutcome.ADAPTER_ERROR),
        (asyncio.TimeoutError(), ObservationOutcome.TIMEOUT),
    ],
)
def test_unsaved_observation_terminal_paths_revoke_before_settling(
    tmp_path,
    adapter_result,
    expected_outcome,
):
    adapter = FakeAdapter()
    if isinstance(adapter_result, SourceObservation):
        adapter.observation = adapter_result
    else:
        adapter.observation_error = adapter_result
    service, _, _ = _service(tmp_path, ModelHubConfig(), adapter)

    result = asyncio.run(
        service.observe_source(
            {
                "vendor": "anthropic",
                "base_url": None,
                "key": "sk-test-terminal-cleanup",
            }
        )
    )

    assert result["observation"]["outcome"] == expected_outcome.value
    assert adapter.revoked == ["cred_00000001"]


def test_service_accepts_authoritative_reachable_adapter_error(tmp_path):
    adapter = FakeAdapter()
    adapter.observation = SourceObservation(
        outcome=ObservationOutcome.ADAPTER_ERROR,
        reachable=True,
        authenticated=None,
        protocol=None,
        discovery=ObservationDiscovery.NOT_ATTEMPTED,
        models=(),
    )
    service, _, _ = _service(tmp_path, ModelHubConfig(), adapter)

    result = asyncio.run(
        service.observe_source(
            {
                "vendor": "custom",
                "base_url": "https://relay.example/v1",
                "key": "sk-test-reachable-adapter-error",
            }
        )
    )

    assert result["observation"] == {
        "contract_version": 8,
        "outcome": "adapter_error",
        "reachable": True,
        "authenticated": "unknown",
        "protocol": None,
        "discovery": "not_attempted",
        "models": [],
        "model_metadata": [],
    }


def test_unknown_adapter_error_does_not_claim_connection(tmp_path):
    adapter = FakeAdapter()
    adapter.observation_error = RuntimeError("injected adapter failure")
    service, store, _ = _service(tmp_path, ModelHubConfig(), adapter)

    with pytest.raises(ModelHubError) as exc:
        asyncio.run(
            service.create_source(
                {
                    "kind": "api_key",
                    "vendor": "custom",
                    "display_name": "Unknown adapter failure",
                    "key": "sk-test-unknown-adapter-error",
                }
            )
        )

    assert exc.value.code == "discovery_failed"
    assert exc.value.detail == "modelHub.errors.adapter_error"
    assert exc.value.data["observation"] == {
        "contract_version": 8,
        "outcome": "adapter_error",
        "reachable": None,
        "authenticated": "unknown",
        "protocol": None,
        "discovery": "not_attempted",
        "models": [],
        "model_metadata": [],
    }
    assert store.load().sources == []
    assert adapter.revoked == ["cred_00000001"]


def test_observation_terminal_legality_has_no_service_or_runtime_copy():
    from ast import AsyncFunctionDef, Attribute, Call, FunctionDef, Name, parse, walk
    from pathlib import Path

    root = Path(__file__).parents[1]
    service_tree = parse(
        (root / "core/handlers/model_hub/service.py").read_text(encoding="utf-8")
    )
    runtime_tree = parse(
        (root / "vibe/model_hub_runtime/adapter.py").read_text(encoding="utf-8")
    )

    def calls(function) -> set[str]:
        return {
            node.func.id if isinstance(node.func, Name) else node.func.attr
            for node in walk(function)
            if isinstance(node, Call) and isinstance(node.func, (Name, Attribute))
        }

    validator = next(
        node
        for node in walk(service_tree)
        if isinstance(node, FunctionDef) and node.name == "_validate_observation"
    )
    producer = next(
        node
        for node in walk(runtime_tree)
        if isinstance(node, AsyncFunctionDef) and node.name == "observe_source"
    )
    assert "validate_source_observation" in calls(validator)
    assert "make_source_observation" in calls(producer)
    assert "SourceObservation" not in calls(producer)
    assert not any(
        isinstance(node, Attribute)
        and isinstance(node.value, Name)
        and node.value.id == "ObservationOutcome"
        for node in walk(validator)
    )


def test_unsaved_observation_cancellation_revokes_before_settling(tmp_path):
    adapter = FakeAdapter()
    adapter.discovery_started = asyncio.Event()
    adapter.discovery_block = asyncio.Event()
    service, store, _ = _service(tmp_path, ModelHubConfig(), adapter)

    async def scenario():
        task = asyncio.create_task(
            service.observe_source(
                {
                    "vendor": "anthropic",
                    "base_url": None,
                    "key": "sk-test-observation-cancel",
                }
            )
        )
        await adapter.discovery_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert store.load().sources == []
    assert adapter.revoked == ["cred_00000001"]
    assert service.revocations.list() == []


def test_unsaved_observation_cancellation_during_cleanup_waits_then_raises(tmp_path):
    adapter = FakeAdapter()
    adapter.revoke_started = asyncio.Event()
    adapter.revoke_block = asyncio.Event()
    service, store, _ = _service(tmp_path, ModelHubConfig(), adapter)

    async def scenario():
        task = asyncio.create_task(
            service.create_source(
                {
                    "kind": "api_key",
                    "vendor": "anthropic",
                    "display_name": "Key",
                    "key": "sk-test-cleanup-cancel",
                }
            )
        )
        await adapter.revoke_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        adapter.revoke_block.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert store.load().sources == []
    assert adapter.revoked == ["cred_00000001"]
    assert service.revocations.list() == []


def test_unsaved_observation_revoke_failure_is_journaled_and_reconciled(tmp_path):
    adapter = FakeAdapter()
    adapter.revoke_error = True
    service, store, _ = _service(tmp_path, ModelHubConfig(), adapter)

    result = asyncio.run(
        service.observe_source(
            {
                "vendor": "anthropic",
                "base_url": None,
                "key": "sk-test-observation-revoke",
            }
        )
    )
    assert result["observation"]["protocol"] == "anthropic"
    assert "credential_ref" not in result["observation"]
    pending = service.revocations.list()
    assert len(pending) == 1
    assert pending[0].credential_ref == "cred_00000001"

    adapter.revoke_error = False
    repaired, _, _ = _service(tmp_path, store.load(), adapter)
    asyncio.run(repaired._ensure_engine_synced())
    assert repaired.revocations.list() == []
    assert adapter.revoked == ["cred_00000001"]


def test_unsaved_observation_fails_when_cleanup_is_not_durable(tmp_path):
    adapter = FakeAdapter()
    adapter.revoke_error = True
    service, store, _ = _service(tmp_path, ModelHubConfig(), adapter)

    def fail_journal_write(*_args, **_kwargs):
        raise OSError("journal is unavailable")

    service.revocations.add = fail_journal_write

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(
            service.observe_source(
                {
                    "vendor": "anthropic",
                    "base_url": None,
                    "key": "sk-test-observation-cleanup-failure",
                }
            )
        )

    assert exc_info.value.code == "engine_down"
    assert store.load().sources == []
    assert service.revocations.list() == []


def test_credential_cleanup_settlement_has_one_durable_boundary():
    from ast import AsyncFunctionDef, Attribute, Call, parse, walk
    from pathlib import Path

    tree = parse(
        (Path(__file__).parents[1] / "core/handlers/model_hub/service.py").read_text(
            encoding="utf-8"
        )
    )
    raw_cleanup_callers = {
        function.name
        for function in walk(tree)
        if isinstance(function, AsyncFunctionDef)
        and any(
            isinstance(node, Call)
            and isinstance(node.func, Attribute)
            and node.func.attr == "_rollback_credential"
            for node in walk(function)
        )
    }

    assert raw_cleanup_callers == {"_require_credential_cleanup"}


def test_every_persisting_source_path_requires_response_backed_protocol_evidence():
    from ast import AsyncFunctionDef, Attribute, Call, Name, parse, walk
    from pathlib import Path

    root = Path(__file__).parents[1]
    production_trees = [
        parse(path.read_text(encoding="utf-8"), filename=str(path))
        for directory in ("config", "core", "modules", "vibe")
        for path in (root / directory).rglob("*.py")
    ]
    assert not any(
        (
            isinstance(node, Name)
            and node.id == "_default_protocol"
        )
        or (
            isinstance(node, Attribute)
            and node.attr == "_default_protocol"
        )
        for tree in production_trees
        for node in walk(tree)
    )

    def async_functions(path: Path) -> dict[str, AsyncFunctionDef]:
        tree = parse(path.read_text(encoding="utf-8"), filename=str(path))
        return {
            node.name: node
            for node in walk(tree)
            if isinstance(node, AsyncFunctionDef)
        }

    def calls(function: AsyncFunctionDef) -> set[str]:
        return {
            (
                node.func.id
                if isinstance(node.func, Name)
                else node.func.attr
            )
            for node in walk(function)
            if isinstance(node, Call)
            and isinstance(node.func, (Name, Attribute))
        }

    service_functions = async_functions(
        root / "core/handlers/model_hub/service.py"
    )
    migration_functions = async_functions(
        root / "core/handlers/model_hub/migration.py"
    )

    assert "_require_proven_source_payload" in calls(
        service_functions["create_source"]
    )
    assert "_require_proven_source_payload" in calls(
        migration_functions["apply_native_migration"]
    )
    assert "_require_proven_observation" in calls(
        service_functions["_create_oauth_source"]
    )


def test_manual_model_delete_ignores_preexisting_unrelated_gap(tmp_path):
    source = _source("src_manual001")
    source.models.append(
        ModelHubModelConfig(id="manual-model", provenance="manual")
    )
    broken = _source("src_manual002", ("other-model",), status="cooldown")
    broken.state = ModelHubSourceStateConfig(
        status="cooldown",
        retry_at="2099-01-01T00:00:00Z",
        detail_key="models.source.cooldown.rate_limited",
    )
    config = _config([source, broken])
    config.agents["claude"].routes["claude-sonnet-4-6"] = ModelHubRouteConfig(
        hops=(ModelHubRouteHopConfig(broken.id, "other-model"),)
    )
    service, store, _ = _service(tmp_path, config)

    result = asyncio.run(service.delete_custom_model(source.id, "manual-model"))

    assert result["removed_hops"] == []
    assert result["interrupted"] == []
    assert [model.id for model in store.load().sources[0].models] == [
        "claude-opus-4-6"
    ]


def test_manual_model_delete_requires_the_exact_guard_plan(tmp_path):
    menu_model = "claude-opus-4-6"
    source = _source("src_manual003")
    source.models.append(ModelHubModelConfig(id="manual-model", provenance="manual"))
    config = _config([source], model=menu_model)
    config.agents["claude"].routes[menu_model] = ModelHubRouteConfig(
        hops=(ModelHubRouteHopConfig(source.id, "manual-model"),)
    )
    service, store, _ = _service(tmp_path, config)

    with pytest.raises(ModelHubError) as exc:
        asyncio.run(service.delete_custom_model(source.id, "manual-model"))

    assert exc.value.code == "source_model_in_route_chain"
    assert store.load().to_payload() == config.to_payload()
    with pytest.raises(ModelHubError) as unconfirmed:
        asyncio.run(
            service.delete_custom_model(
                source.id,
                "manual-model",
                force=True,
            )
        )
    assert unconfirmed.value.data == exc.value.data
    result = asyncio.run(
        service.delete_custom_model(
            source.id,
            "manual-model",
            force=True,
            confirmed_remove_hops=exc.value.data["would_remove_hops"],
            confirmed_interruptions=exc.value.data["would_interrupt"],
        )
    )
    assert result["removed_hops"] == exc.value.data["would_remove_hops"]
    assert store.load().agents["claude"].routes[menu_model].hops == ()


def test_delete_unused_source_ignores_preexisting_unrelated_gap(tmp_path):
    menu_model = "claude-opus-4-6"
    healthy = _source("src_delete02", (menu_model,))
    unused = _source("src_delete03", ("unused-model",))
    broken = _source("src_delete04", ("other-model",), status="cooldown")
    broken.state = ModelHubSourceStateConfig(
        status="cooldown",
        retry_at="2099-01-01T00:00:00Z",
        detail_key="models.source.cooldown.rate_limited",
    )
    config = _config([healthy], model=menu_model)
    config.sources.extend((unused, broken))
    config.agents["claude"].routes["claude-sonnet-4-6"] = ModelHubRouteConfig(
        hops=(ModelHubRouteHopConfig(broken.id, "other-model"),)
    )
    service, store, _ = _service(tmp_path, config)

    result = asyncio.run(service.delete_source(unused.id))

    assert result == {"removed_hops": [], "interrupted": []}
    assert {source.id for source in store.load().sources} == {healthy.id, broken.id}


def test_credential_replace_ignores_preexisting_unrelated_gap(tmp_path):
    menu_model = "claude-opus-4-6"
    healthy = _source("src_replace01", (menu_model,))
    broken = _source("src_replace02", ("other-model",), status="cooldown")
    broken.state = ModelHubSourceStateConfig(
        status="cooldown",
        retry_at="2099-01-01T00:00:00Z",
        detail_key="models.source.cooldown.rate_limited",
    )
    config = _config([healthy], model=menu_model)
    config.sources.append(broken)
    config.agents["claude"].routes["claude-sonnet-4-6"] = ModelHubRouteConfig(
        hops=(ModelHubRouteHopConfig(broken.id, "other-model"),)
    )
    service, _store, adapter = _service(
        tmp_path,
        config,
        FakeAdapter(discovered=(menu_model,)),
    )

    result = asyncio.run(
        service.replace_credential(healthy.id, {"key": "replacement-key"})
    )

    assert result["removed_hops"] == []
    assert result["interrupted"] == []
    assert adapter.provisioned == ["replacement-key"]


def test_delete_source_reports_and_then_prunes_exact_hops(tmp_path):
    menu_model = "claude-opus-4-6"
    source = _source("src_delete01", (menu_model,))
    config = _config([source], model=menu_model)
    service, store, _ = _service(tmp_path, config)
    with pytest.raises(ModelHubError) as exc:
        asyncio.run(service.delete_source(source.id))
    assert exc.value.code == "source_in_route_chain"
    assert exc.value.data["would_remove_hops"][0]["model_id"] == menu_model
    with pytest.raises(ModelHubError) as unconfirmed:
        asyncio.run(service.delete_source(source.id, force=True))
    assert unconfirmed.value.code == exc.value.code
    assert unconfirmed.value.data == exc.value.data
    result = asyncio.run(
        service.delete_source(
            source.id,
            force=True,
            confirmed_remove_hops=exc.value.data["would_remove_hops"],
            confirmed_interruptions=exc.value.data["would_interrupt"],
        )
    )
    assert result["removed_hops"]
    assert store.load().sources == []
    assert store.load().agents["claude"].routes[menu_model].hops == ()


def test_refresh_source_uses_guarded_success_shape(tmp_path):
    menu_model = "claude-opus-4-6"
    source = _source("src_refresh1", (menu_model,))
    config = _config([source], model=menu_model)
    adapter = FakeAdapter(discovered=("new-model",))
    service, store, _ = _service(tmp_path, config, adapter)
    with pytest.raises(ModelHubError) as exc:
        asyncio.run(service.refresh_source(source.id))
    assert exc.value.code == "source_model_in_route_chain"
    result = asyncio.run(
        service.refresh_source(
            source.id,
            force=True,
            confirmed_remove_hops=exc.value.data["would_remove_hops"],
            confirmed_interruptions=exc.value.data["would_interrupt"],
        )
    )
    assert set(result) == {"source", "removed_hops", "interrupted"}
    assert result["removed_hops"][0]["model_id"] == menu_model
    assert store.load().agents["claude"].routes[menu_model].hops == ()


@pytest.mark.parametrize(
    ("inventory_case", "discovered"),
    [
        ("normal", ("claude-opus-4-6", "new-model")),
        ("empty", ()),
        ("failure", None),
    ],
)
@pytest.mark.parametrize("operation", ["refresh", "base_url", "credential"])
def test_inventory_mutations_share_the_successful_discovery_finalizer(
    tmp_path,
    operation,
    inventory_case,
    discovered,
):
    source = _source(
        "src_finalize1",
        ("claude-opus-4-6",),
        vendor="custom",
    )
    source.base_url = "https://old-relay.example/v1"
    source.state = ModelHubSourceStateConfig(
        status="error",
        detail_key="models.source.error.unclassified",
    )
    config = _config([source])
    adapter = FakeAdapter(discovered=discovered or ())
    if inventory_case == "failure":
        adapter.discovery_error = ModelDiscoveryError("injected discovery failure")
    service, store, _ = _service(tmp_path, config, adapter)
    before = store.load().to_payload()

    def mutation(confirmation=None):
        confirmation = confirmation or {}
        if operation == "refresh":
            return service.refresh_source(source.id, **confirmation)
        if operation == "base_url":
            return service.patch_source(
                source.id,
                {
                    "base_url": "https://new-relay.example/v1",
                    **confirmation,
                },
            )
        return service.replace_credential(
            source.id,
            {"key": "replacement-key", **confirmation},
        )

    if inventory_case == "failure":
        with pytest.raises(ModelHubError) as exc:
            asyncio.run(mutation())
        assert exc.value.code == "discovery_failed"
        assert store.load().to_payload() == before
        return

    if inventory_case == "empty":
        with pytest.raises(ModelHubError) as refusal:
            asyncio.run(mutation())
        confirmed = {
            "force": True,
            "would_remove_hops": refusal.value.data["would_remove_hops"],
            "would_interrupt": refusal.value.data["would_interrupt"],
        }
        if operation == "refresh":
            confirmed = {
                "force": True,
                "confirmed_remove_hops": refusal.value.data[
                    "would_remove_hops"
                ],
                "confirmed_interruptions": refusal.value.data[
                    "would_interrupt"
                ],
            }
        result = asyncio.run(mutation(confirmed))
    else:
        result = asyncio.run(mutation())
    persisted = store.load().sources[0]
    response_source = result["source"]
    assert [model["retired"] for model in response_source["models"]] == [
        False
    ] * len(discovered)
    assert response_source["adopted_by"] == (
        [{"backend": "claude", "menu_model": "claude-opus-4-6"}]
        if inventory_case == "normal"
        else []
    )
    persisted_projection = {
        key: value
        for key, value in response_source.items()
        if key != "adopted_by"
    }
    for model in persisted_projection["models"]:
        model.pop("retired")
    assert persisted_projection == persisted.to_payload()
    assert persisted.state == ModelHubSourceStateConfig(status="standby")
    assert persisted.last_discovered_at is not None
    assert [model.id for model in persisted.models] == list(discovered)


def test_all_interruption_guards_use_the_shared_baseline_comparator():
    from ast import Attribute, AsyncFunctionDef, Name, parse, walk
    from pathlib import Path

    tree = parse(
        (Path(__file__).parents[1] / "core/handlers/model_hub/service.py").read_text(
            encoding="utf-8"
        )
    )
    direct_baseline_guards = {
        "delete_source",
        "set_agent_chain",
        "delete_custom_model",
    }
    inventory_guards = {
        "patch_source",
        "replace_credential",
        "refresh_source",
    }
    methods = {
        node.name: node
        for node in walk(tree)
        if isinstance(node, AsyncFunctionDef)
        and node.name in direct_baseline_guards | inventory_guards
    }

    def calls(method):
        return [
            (
                node.func.id
                if isinstance(node.func, Name)
                else node.func.attr
                if isinstance(node.func, Attribute)
                else None
            )
            for node in walk(method)
            if node.__class__.__name__ == "Call"
        ]

    inventory_finalizer = next(
        node
        for node in walk(tree)
        if node.__class__.__name__ == "AsyncFunctionDef"
        and node.name == "_finalize_successful_discovery"
    )
    assert "_guard_inventory_mutation" in calls(inventory_finalizer)
    assert "_apply_discovered_models" in calls(inventory_finalizer)
    assert "_commit_synced" in calls(inventory_finalizer)

    for name in inventory_guards:
        assert "_finalize_successful_discovery" in calls(methods[name])
        assert "_guard_inventory_mutation" not in calls(methods[name])
        assert "_apply_discovered_models" not in calls(methods[name])
        assert "_introduced_interruptions" not in calls(methods[name])

    for name in direct_baseline_guards:
        assert "_introduced_interruptions" in calls(methods[name])
        assert "_would_interrupt" not in calls(methods[name])


def test_credential_target_and_refresh_capability_have_single_service_consumers():
    from ast import AsyncFunctionDef, Attribute, Call, Name, parse, walk
    from pathlib import Path

    tree = parse(
        (Path(__file__).parents[1] / "core/handlers/model_hub/service.py").read_text(
            encoding="utf-8"
        )
    )
    methods = {
        node.name: node
        for node in walk(tree)
        if isinstance(node, AsyncFunctionDef)
    }

    def calls(method: AsyncFunctionDef) -> set[str]:
        return {
            node.func.id
            if isinstance(node.func, Name)
            else node.func.attr
            for node in walk(method)
            if isinstance(node, Call)
            and isinstance(node.func, (Name, Attribute))
        }

    assert "retarget_api_key_credential" in calls(methods["patch_source"])
    assert "_classify_credential_outcome" in calls(
        methods["_classify_source_outcome"]
    )
    assert "credential_supports_refresh" in calls(
        methods["_classify_credential_outcome"]
    )
    assert "_classify_source_outcome" in calls(methods["probe_agent"])
    assert "_classify_source_outcome" in calls(methods["resolve"])
    capability_callers = {
        name
        for name, method in methods.items()
        if "credential_supports_refresh" in calls(method)
    }
    assert capability_callers == {"_classify_credential_outcome"}


def test_direct_mode_refuses_chain_and_probe(tmp_path):
    config = ModelHubConfig()
    service, _, _ = _service(tmp_path, config)
    asyncio.run(service.set_agent_mode("claude", "direct"))
    with pytest.raises(ModelHubError) as chain_error:
        service.agent_chain("claude", "claude-opus-4-6")
    assert chain_error.value.code == "direct_mode"
    with pytest.raises(ModelHubError) as probe_error:
        asyncio.run(service.probe_agent("claude", "claude-opus-4-6"))
    assert probe_error.value.code == "direct_mode"
