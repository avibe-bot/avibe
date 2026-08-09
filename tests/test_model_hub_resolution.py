from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from config.v2_config import (
    ModelHubAgentSupplyConfig,
    ModelHubConfig,
    ModelHubMenuConfig,
    ModelHubModelConfig,
    ModelHubRouteConfig,
    ModelHubRouteHopConfig,
    ModelHubSourceConfig,
    ModelHubSourceStateConfig,
)
from core.handlers.model_hub.adapter import (
    EngineHealth,
    EngineStatus,
    ObservationDiscovery,
    ObservationOutcome,
    RawCallOutcome,
    RawOutcomeKind,
    SourceObservation,
)
from core.handlers.model_hub.events import BoundedEventLog
from core.handlers.model_hub.resolver import resolve_model_hub_turn
from core.handlers.model_hub.revocations import CredentialRevocationJournal
from core.handlers.model_hub.service import (
    ModelHubError,
    ModelHubService,
    _matching_v1_model_id,
)


class MemoryStore:
    def __init__(self, config: ModelHubConfig):
        self.config = config
        self.requested_models = {"claude": "claude-opus-4-6"}

    def load(self) -> ModelHubConfig:
        return self.config

    def save(self, config: ModelHubConfig) -> None:
        self.config = config

    def requested_model(self, backend: str) -> str:
        return self.requested_models.get(backend, "")


class FakeInvokeHandle:
    def __init__(self, outcome: RawCallOutcome):
        self._outcome = outcome
        self.stream = None

    async def outcome(self) -> RawCallOutcome:
        return self._outcome


class FakeAdapter:
    def __init__(self, discovered: tuple[str, ...] = ("claude-opus-4-6",)):
        self.discovered = discovered
        self.observation: SourceObservation | None = None
        self.observation_error: BaseException | None = None
        self.observed_protocol_orders: list[tuple[str, ...]] = []
        self.revoked: list[str] = []
        self.revoke_error = False
        self.provisioned: list[str] = []
        self.synced: list[tuple] = []
        self.discovery_started: asyncio.Event | None = None
        self.discovery_block: asyncio.Event | None = None
        self.sync_started: asyncio.Event | None = None
        self.sync_block: asyncio.Event | None = None
        self.outcomes = deque()

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
        return f"cred_{len(self.provisioned):08d}"

    async def revoke_credential(self, credential_ref):
        if self.revoke_error:
            raise RuntimeError("injected revoke failure")
        self.revoked.append(credential_ref)

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
        return self.discovered

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
            model_ids=self.discovered,
        )

    async def invoke(self, source_id, model_id, request, stream, origin):
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
        ("claude-opus-4-5-20251101", "claude-opus-4-6-20260101", "claude-opus-4-6-20260115"),
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


def test_matching_v1_opencode_bare_name_requires_unique_checked_suffix():
    source = _source("src_openv001", ("x",), vendor="custom")
    assert _matching_v1_model_id(backend="opencode", requested_model="x", source=source, checked_models=("custom/x",)) == "x"
    assert _matching_v1_model_id(backend="opencode", requested_model="x", source=source, checked_models=("a/x", "custom/x")) is None


def test_runtime_opencode_resolution_never_repeats_bare_name_matching():
    source = _source("src_openv001", ("x",), vendor="custom")
    config = _config([source], model="custom/x")
    agent = config.agents["opencode"]
    agent.menu = ModelHubMenuConfig(view="featured", checked=["custom/x"])
    agent.routes["custom/x"] = ModelHubRouteConfig(
        hops=(ModelHubRouteHopConfig(source.id, "x"),)
    )

    exact = resolve_model_hub_turn(config, "opencode", "custom/x")
    bare = resolve_model_hub_turn(config, "opencode", "x")

    assert exact.source is source
    assert exact.target_model == "x"
    assert bare.source is None


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
    assert payload["supply_state"] == "interrupted"


def test_set_agent_chain_returns_guarded_exact_route(tmp_path):
    first = _source("src_chain002", ("requested",))
    second = _source("src_chain003", ("requested",))
    config = _config([first, second], model="requested")
    service, store, _ = _service(tmp_path, config)
    result = asyncio.run(
        service.set_agent_chain(
            "claude",
            "requested",
            {
                "hops": [
                    {"source_id": second.id, "model_id": "requested"},
                    {"source_id": first.id, "model_id": "requested"},
                ],
                "force": True,
            },
        )
    )
    assert result["removed_hops"] == []
    assert isinstance(result["interrupted"], list)
    assert [item["source_id"] for item in result["chain"]["chain"]] == [second.id, first.id]
    assert [hop.source_id for hop in store.load().agents["claude"].routes["requested"].hops] == [second.id, first.id]


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
                "protocol_order": [
                    "openai_responses",
                    "anthropic",
                    "openai_chat",
                ],
            }
        )
    )

    assert adapter.observed_protocol_orders == [
        ("openai_responses", "anthropic", "openai_chat")
    ]
    assert result["source"]["protocol"] == "openai_responses"
    assert store.load().sources[0].protocol == "openai_responses"
    assert adapter.revoked == ["cred_00000001"]


def test_ambiguous_observation_never_creates_a_source(tmp_path):
    adapter = FakeAdapter()
    adapter.observation = SourceObservation(
        outcome=ObservationOutcome.AMBIGUOUS,
        reachable=True,
        authenticated=True,
        protocol=None,
        discovery=ObservationDiscovery.NOT_ATTEMPTED,
        model_ids=(),
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


@pytest.mark.parametrize(
    ("adapter_result", "expected_outcome"),
    [
        (
            SourceObservation(
                outcome=ObservationOutcome.AUTHENTICATION_FAILED,
                reachable=True,
                authenticated=False,
                protocol="anthropic",
                discovery=ObservationDiscovery.NOT_ATTEMPTED,
                model_ids=(),
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


def test_delete_source_reports_and_then_prunes_exact_hops(tmp_path):
    source = _source("src_delete01", ("requested",))
    config = _config([source], model="requested")
    service, store, _ = _service(tmp_path, config)
    with pytest.raises(ModelHubError) as exc:
        asyncio.run(service.delete_source(source.id))
    assert exc.value.code == "source_in_route_chain"
    assert exc.value.data["would_remove_hops"][0]["model_id"] == "requested"
    result = asyncio.run(service.delete_source(source.id, force=True))
    assert result["removed_hops"]
    assert store.load().sources == []
    assert store.load().agents["claude"].routes["requested"].hops == ()


def test_refresh_source_uses_guarded_success_shape(tmp_path):
    source = _source("src_refresh1", ("requested",))
    config = _config([source], model="requested")
    adapter = FakeAdapter(discovered=("new-model",))
    service, store, _ = _service(tmp_path, config, adapter)
    with pytest.raises(ModelHubError) as exc:
        asyncio.run(service.refresh_source(source.id))
    assert exc.value.code == "source_model_in_route_chain"
    result = asyncio.run(service.refresh_source(source.id, force=True))
    assert set(result) == {"source", "removed_hops", "interrupted"}
    assert result["removed_hops"][0]["model_id"] == "requested"
    assert store.load().agents["claude"].routes["requested"].hops == ()


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
