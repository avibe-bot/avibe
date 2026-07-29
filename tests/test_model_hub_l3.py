from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from jsonschema import Draft7Validator, FormatChecker

from config.v2_config import (
    ModelHubAgentSourcesConfig,
    ModelHubAgentSupplyConfig,
    ModelHubConfig,
    ModelHubModelConfig,
    ModelHubSourceConfig,
    ModelHubSourceStateConfig,
)
from core.handlers.model_hub.adapter import (
    EngineHealth,
    EngineStatus,
    RawCallOutcome,
    RawOutcomeKind,
)
from core.handlers.model_hub.classification import classify_outcome
from core.handlers.model_hub.events import (
    BoundedEventLog,
    build_resolution_event,
)
from core.handlers.model_hub.provenance import (
    BoundedProvenanceStore,
    TurnCorrelationRegistry,
)
from core.handlers.model_hub.revocations import CredentialRevocationJournal
from core.handlers.model_hub.service import ModelHubError, ModelHubService
from core.run_settlement import (
    SETTLED_BY_NO_TERMINAL_RESULT,
    SETTLED_BY_STOPPED,
    SETTLED_BY_TERMINAL_RESULT,
)
from core.services.dispatch import TurnDispatchOutcome
from core.session_turns import SessionTurnManager


CONTRACTS = Path(__file__).parents[1] / "docs" / "plans" / "model-hub-contracts"
NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)


def _assert_valid(schema_name: str, payload: dict) -> None:
    schema = json.loads((CONTRACTS / schema_name).read_text(encoding="utf-8"))
    errors = sorted(
        Draft7Validator(
            schema,
            format_checker=FormatChecker(),
        ).iter_errors(payload),
        key=lambda error: list(error.path),
    )
    assert not errors, [error.message for error in errors]


class MemoryStore:
    def __init__(self, config: ModelHubConfig):
        self.config = config
        self.requested_models = {
            "claude": "shared-model",
            "codex": "shared-model",
            "opencode": "openai/shared-model",
        }

    def load(self) -> ModelHubConfig:
        return self.config

    def save(self, config: ModelHubConfig) -> None:
        self.config = config

    def requested_model(self, backend: str) -> str:
        return self.requested_models.get(backend, "")


class InvokeHandle:
    def __init__(self, outcome: RawCallOutcome):
        self._outcome = outcome

    @property
    def stream(self):
        return None

    async def outcome(self) -> RawCallOutcome:
        return self._outcome


class ProbeAdapter:
    def __init__(self, outcomes: list[RawCallOutcome]):
        self.outcomes = deque(outcomes)
        self.invocations: list[tuple[str, str, str]] = []

    async def sync_sources(self, _bindings) -> None:
        return None

    async def revoke_credential(self, _credential_ref: str) -> None:
        return None

    async def invoke(self, source_id, model_id, _request, _stream, origin):
        self.invocations.append((source_id, model_id, origin))
        return InvokeHandle(self.outcomes.popleft())

    async def status(self) -> EngineStatus:
        return EngineStatus(
            health=EngineHealth.OK,
            installed_version="test",
            verified=True,
            listen_host="127.0.0.1",
            listen_port=15220,
            last_check_iso=NOW.isoformat(),
        )


def _source(
    source_id: str,
    label: str,
    *,
    channel: str = "hub",
    status: str = "standby",
    retry_at: str | None = None,
) -> ModelHubSourceConfig:
    return ModelHubSourceConfig(
        id=source_id,
        kind="subscription" if channel == "native_cli" else "api_key",
        vendor="openai",
        display_name=label,
        protocol="openai_responses",
        supply_channel=channel,
        billing="monthly" if channel == "native_cli" else "metered",
        state=ModelHubSourceStateConfig(
            status=status,
            retry_at=retry_at,
            detail_key=(
                "models.source.cooldown.rate_limited"
                if status == "cooldown"
                else None
            ),
        ),
        models=[
            ModelHubModelConfig(
                id="shared-model",
                provenance="discovered",
            )
        ],
        credential_ref=None if channel == "native_cli" else f"cred_{source_id}",
    )


def _config(sources: list[ModelHubSourceConfig]) -> ModelHubConfig:
    agents = {
        backend: ModelHubAgentSupplyConfig.default(backend, mode="hub")
        for backend in ("claude", "codex", "opencode")
    }
    for agent in agents.values():
        agent.sources = ModelHubAgentSourcesConfig(
            policy="custom",
            order=[source.id for source in sources],
        )
    agents["opencode"].menu.checked = ["openai/shared-model"]
    return ModelHubConfig(sources=sources, agents=agents)


def _outcome(
    kind: RawOutcomeKind,
    *,
    status: int | None = None,
    code: str | None = None,
    message: str | None = None,
    source_id: str = "src_primary01",
) -> RawCallOutcome:
    return RawCallOutcome(
        kind=kind,
        http_status=status,
        error_code=code,
        redacted_message=message,
        stream_started=False,
        model_id="shared-model",
        source_id=source_id,
    )


def _service(
    tmp_path: Path,
    *,
    sources: list[ModelHubSourceConfig],
    outcomes: list[RawCallOutcome] | None = None,
) -> ModelHubService:
    return ModelHubService(
        store=MemoryStore(_config(sources)),
        adapter=ProbeAdapter(outcomes or []),
        events=BoundedEventLog(tmp_path / "events.json"),
        provenance=BoundedProvenanceStore(tmp_path / "provenance.json"),
        revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
        now=lambda: NOW,
    )


def _begin_hub_attempt(
    registry: TurnCorrelationRegistry,
    *,
    turn_id: str,
    scope: str = "/repo",
) -> str | None:
    token = registry.credentials("codex", scope, turn_id)
    exact_turn = registry.begin_gateway_request(
        backend="codex",
        token=token,
        requested_model_id="shared-model",
    )
    registry.begin_attempt(
        exact_turn,
        source_id="src_primary01",
        resolved_model_id="shared-model",
        channel="hub",
        via_mapping=False,
    )
    return exact_turn


def test_process_credentials_record_only_exact_turns(tmp_path: Path) -> None:
    store = BoundedProvenanceStore(tmp_path / "provenance.json")
    registry = TurnCorrelationRegistry(store)

    token = registry.credentials("codex", "/repo", "turn_exact")
    assert token == registry.credentials("codex", "/repo", "turn_exact")
    assert token != registry.credentials("codex", "/other", "turn_other")
    assert registry.authenticates("codex", token) is True
    assert registry.authenticates("claude", token) is False
    assert registry.credentials("claude", "session-a", "turn_a") != registry.credentials(
        "claude",
        "session-b",
        "turn_b",
    )

    exact_turn = registry.begin_gateway_request(
        backend="codex",
        token=token,
        requested_model_id="shared-model",
    )
    registry.begin_attempt(
        exact_turn,
        source_id="src_primary01",
        resolved_model_id="shared-model",
        channel="hub",
        via_mapping=False,
    )
    success = _outcome(RawOutcomeKind.SUCCESS)
    registry.finish_attempt(
        exact_turn,
        outcome=success,
        decision=classify_outcome(success),
    )
    registry.settle(
        "turn_exact",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )

    record = store.get("turn_exact")
    assert record is not None
    assert record["outcome"] == "served"
    _assert_valid("turn-provenance.schema.json", record)


def test_fsm_drop_overrides_a_completed_gateway_attempt(tmp_path: Path) -> None:
    store = BoundedProvenanceStore(tmp_path / "provenance.json")
    registry = TurnCorrelationRegistry(store)
    exact_turn = _begin_hub_attempt(registry, turn_id="turn_drop")
    success = _outcome(RawOutcomeKind.SUCCESS)
    registry.finish_attempt(
        exact_turn,
        outcome=success,
        decision=classify_outcome(success),
    )

    registry.settle(
        "turn_drop",
        settled_by=SETTLED_BY_NO_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )

    record = store.get("turn_drop")
    assert record is not None
    assert record["outcome"] == "failed_terminal"
    assert record["terminal_error"]["reason"] == "stream_interrupted"
    assert record["served"] is None
    _assert_valid("turn-provenance.schema.json", record)


def test_ambiguous_mixed_and_opencode_attempts_leave_no_record(
    tmp_path: Path,
) -> None:
    store = BoundedProvenanceStore(tmp_path / "provenance.json")
    registry = TurnCorrelationRegistry(store)

    first = _begin_hub_attempt(registry, turn_id="turn_first")
    assert first == "turn_first"
    registry.credentials("codex", "/repo", "turn_second")
    assert (
        registry.begin_gateway_request(
            backend="codex",
            token=registry.credentials("codex", "/repo", "turn_second"),
            requested_model_id="shared-model",
        )
        is None
    )
    registry.settle("turn_first", settled_by=SETTLED_BY_STOPPED)
    registry.settle("turn_second", settled_by=SETTLED_BY_TERMINAL_RESULT)
    assert store.get("turn_first") is None
    assert store.get("turn_second") is None

    mixed = _begin_hub_attempt(
        registry,
        turn_id="turn_mixed",
        scope="/mixed",
    )
    registry.credentials("codex", "/mixed", None)
    success = _outcome(RawOutcomeKind.SUCCESS)
    registry.finish_attempt(
        mixed,
        outcome=success,
        decision=classify_outcome(success),
    )
    registry.settle("turn_mixed", settled_by=SETTLED_BY_TERMINAL_RESULT)
    assert store.get("turn_mixed") is None

    opencode_token = registry.credentials(
        "opencode",
        "opencode:shared-server",
        "turn_opencode",
    )
    assert (
        registry.begin_gateway_request(
            backend="opencode",
            token=opencode_token,
            requested_model_id="openai/shared-model",
        )
        is None
    )
    registry.settle("turn_opencode", settled_by=SETTLED_BY_TERMINAL_RESULT)
    assert store.get("turn_opencode") is None


def test_fsm_cancel_drop_and_control_classification(tmp_path: Path) -> None:
    async def exercise(
        *,
        turn_id: str,
        mode: str,
    ) -> dict | None:
        store = BoundedProvenanceStore(tmp_path / f"{turn_id}.json")
        registry = TurnCorrelationRegistry(store)
        runtime = SimpleNamespace(
            settle_turn=lambda value, *, settled_by, ts: registry.settle(
                value,
                settled_by=settled_by,
                ts=ts,
            )
        )
        controller = SimpleNamespace(
            model_hub_runtime=runtime,
            command_handler=SimpleNamespace(
                handle_stop=AsyncMock(return_value=True)
            ),
            set_agent_status=lambda _session_id, _status: None,
        )
        manager = SessionTurnManager(controller)
        manager.flush_queue = AsyncMock(return_value=False)
        started = asyncio.Event()
        hold = asyncio.Event()

        async def dispatch(_controller, _context, _text, **_kwargs):
            current_turn = manager.model_hub_turn_id_for_task()
            assert current_turn == turn_id
            exact_turn = _begin_hub_attempt(
                registry,
                turn_id=current_turn,
            )
            started.set()
            if mode == "cancel":
                await hold.wait()
            if mode == "served":
                success = _outcome(RawOutcomeKind.SUCCESS)
                registry.finish_attempt(
                    exact_turn,
                    outcome=success,
                    decision=classify_outcome(success),
                )
                settlement = SETTLED_BY_TERMINAL_RESULT
            else:
                settlement = SETTLED_BY_NO_TERMINAL_RESULT
            return TurnDispatchOutcome(error=None, settled_by=settlement)

        context = SimpleNamespace(
            platform="avibe",
            platform_specific={
                "turn_token": turn_id,
                "agent_session_target": {"agent_backend": "codex"},
            },
        )
        with patch(
            "core.session_turns.dispatch_turn_with_outcome",
            side_effect=dispatch,
        ):
            await manager._run(turn_id, context, "test")
            task = manager.in_flight[turn_id].task
            await started.wait()
            if mode == "cancel":
                result = await manager.cancel(turn_id)
                assert result["status"] == "cancel_requested"
            await asyncio.gather(task, return_exceptions=True)
        return store.get(turn_id)

    async def run_cases() -> tuple[dict, dict, dict]:
        canceled = await exercise(turn_id="turn_cancel", mode="cancel")
        dropped = await exercise(turn_id="turn_drop", mode="drop")
        served = await exercise(turn_id="turn_control", mode="served")
        assert canceled is not None
        assert dropped is not None
        assert served is not None
        return canceled, dropped, served

    canceled, dropped, served = asyncio.run(run_cases())
    assert canceled["outcome"] == "canceled"
    assert canceled["canceled_attempt"]["source_id"] == "src_primary01"
    assert dropped["outcome"] == "failed_terminal"
    assert dropped["terminal_error"]["reason"] == "stream_interrupted"
    assert served["outcome"] == "served"
    for record in (canceled, dropped, served):
        _assert_valid("turn-provenance.schema.json", record)


def test_same_scope_concurrency_is_absent_and_sequential_control_is_present(
    tmp_path: Path,
) -> None:
    async def run() -> BoundedProvenanceStore:
        store = BoundedProvenanceStore(tmp_path / "concurrency.json")
        registry = TurnCorrelationRegistry(store)
        runtime = SimpleNamespace(
            settle_turn=lambda value, *, settled_by, ts: registry.settle(
                value,
                settled_by=settled_by,
                ts=ts,
            )
        )
        controller = SimpleNamespace(
            model_hub_runtime=runtime,
            command_handler=SimpleNamespace(
                handle_stop=AsyncMock(return_value=True)
            ),
            set_agent_status=lambda _session_id, _status: None,
        )
        manager = SessionTurnManager(controller)
        manager.flush_queue = AsyncMock(return_value=False)
        started = {
            turn_id: asyncio.Event()
            for turn_id in ("turn_one", "turn_two", "turn_sequential")
        }
        release = {
            turn_id: asyncio.Event()
            for turn_id in ("turn_one", "turn_two", "turn_sequential")
        }

        async def dispatch(_controller, context, _text, **_kwargs):
            turn_id = manager.model_hub_turn_id_for_task()
            assert turn_id is not None
            exact_turn = _begin_hub_attempt(
                registry,
                turn_id=turn_id,
                scope="/shared-cwd",
            )
            started[turn_id].set()
            await release[turn_id].wait()
            success = _outcome(RawOutcomeKind.SUCCESS)
            registry.finish_attempt(
                exact_turn,
                outcome=success,
                decision=classify_outcome(success),
            )
            return TurnDispatchOutcome(
                error=None,
                settled_by=SETTLED_BY_TERMINAL_RESULT,
            )

        def context(turn_id: str):
            return SimpleNamespace(
                platform="avibe",
                platform_specific={
                    "turn_token": turn_id,
                    "agent_session_target": {"agent_backend": "codex"},
                },
            )

        with patch(
            "core.session_turns.dispatch_turn_with_outcome",
            side_effect=dispatch,
        ):
            await manager._run("session-one", context("turn_one"), "one")
            first_task = manager.in_flight["session-one"].task
            await manager._run("session-two", context("turn_two"), "two")
            second_task = manager.in_flight["session-two"].task
            await started["turn_one"].wait()
            await started["turn_two"].wait()
            await manager.cancel("session-one")
            release["turn_two"].set()
            await asyncio.gather(
                first_task,
                second_task,
                return_exceptions=True,
            )

            assert store.get("turn_one") is None
            assert store.get("turn_two") is None

            await manager._run(
                "session-sequential",
                context("turn_sequential"),
                "sequential",
            )
            sequential_task = manager.in_flight["session-sequential"].task
            await started["turn_sequential"].wait()
            release["turn_sequential"].set()
            await asyncio.gather(sequential_task, return_exceptions=True)
        return store

    store = asyncio.run(run())
    sequential = store.get("turn_sequential")
    assert sequential is not None
    assert sequential["outcome"] == "served"
    _assert_valid("turn-provenance.schema.json", sequential)


def test_chain_projection_and_probe_latency_partition(tmp_path: Path) -> None:
    cooling = _source(
        "src_cooling01",
        "Cooling",
        status="cooldown",
        retry_at=(NOW + timedelta(minutes=5)).isoformat(),
    )
    primary = _source("src_primary01", "Primary")
    service = _service(
        tmp_path,
        sources=[cooling, primary],
        outcomes=[_outcome(RawOutcomeKind.SUCCESS)],
    )

    chain = service.agent_chain("claude", "shared-model")
    assert [item["source_id"] for item in chain["chain"]] == [
        "src_cooling01",
        "src_primary01",
    ]
    assert chain["supply_state"] == "ok"
    _assert_valid("agent-chain.schema.json", chain)

    probe = asyncio.run(service.probe_agent("claude", "shared-model"))
    assert probe["reachable"] is True
    assert probe["source_id"] == "src_primary01"
    assert isinstance(probe["latency_ms"], int)
    assert probe["error"] is None
    _assert_valid("probe-result.schema.json", probe)

    network_service = _service(
        tmp_path / "network",
        sources=[_source("src_primary01", "Primary")],
        outcomes=[_outcome(RawOutcomeKind.NETWORK_ERROR)],
    )
    network = asyncio.run(
        network_service.probe_agent("claude", "shared-model")
    )
    assert network["reachable"] is False
    assert network["latency_ms"] is None
    assert network["error"] == "models.source.cooldown.network"
    _assert_valid("probe-result.schema.json", network)

    rate_service = _service(
        tmp_path / "rate",
        sources=[_source("src_primary01", "Primary")],
        outcomes=[
            _outcome(
                RawOutcomeKind.HTTP_ERROR,
                status=429,
                code="rate_limited",
            )
        ],
    )
    rate_limited = asyncio.run(
        rate_service.probe_agent("claude", "shared-model")
    )
    assert rate_limited["reachable"] is False
    assert isinstance(rate_limited["latency_ms"], int)
    assert rate_limited["error"] == "models.source.cooldown.rate_limited"
    _assert_valid("probe-result.schema.json", rate_limited)

    unclassified_service = _service(
        tmp_path / "unclassified",
        sources=[_source("src_primary01", "Primary")],
        outcomes=[
            _outcome(
                RawOutcomeKind.HTTP_ERROR,
                status=418,
                code="unexpected_upstream_failure",
            )
        ],
    )
    unclassified = asyncio.run(
        unclassified_service.probe_agent("claude", "shared-model")
    )
    assert unclassified["reachable"] is False
    assert isinstance(unclassified["latency_ms"], int)
    assert unclassified["error"] == "models.source.error.unclassified"
    assert (
        unclassified_service.store.load().sources[0].state.status
        == "error"
    )
    _assert_valid("probe-result.schema.json", unclassified)


def test_probe_no_candidate_and_direct_mode_are_typed(tmp_path: Path) -> None:
    cooling = _source(
        "src_cooling01",
        "Cooling",
        status="cooldown",
        retry_at=(NOW + timedelta(minutes=5)).isoformat(),
    )
    service = _service(tmp_path, sources=[cooling])

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.probe_agent("claude", "shared-model"))
    assert exc_info.value.code == "probe_no_candidate"
    assert exc_info.value.detail == "models.probe.no_candidate.waiting"
    assert exc_info.value.payload == {
        "supply": {
            "supply_state": "waiting",
            "retry_at": cooling.state.retry_at,
        }
    }

    service.store.config.agents["claude"].mode = "direct"
    with pytest.raises(ModelHubError) as chain_error:
        service.agent_chain("claude", "shared-model")
    with pytest.raises(ModelHubError) as probe_error:
        asyncio.run(service.probe_agent("claude", "shared-model"))
    for error in (chain_error.value, probe_error.value):
        assert error.code == "direct_mode"
        assert error.detail == "models.hub.direct_mode"


def test_source_failure_event_is_single_grain_and_retained(tmp_path: Path) -> None:
    source = _source("src_primary01", "Shared paid source")
    service = _service(
        tmp_path,
        sources=[source],
        outcomes=[
            _outcome(
                RawOutcomeKind.HTTP_ERROR,
                status=402,
                code="payment_required",
                message="balance exhausted",
            )
        ],
    )

    probe = asyncio.run(service.probe_agent("claude", "shared-model"))
    assert probe["reachable"] is False
    events = service.list_events(limit=20)
    failures = [event for event in events if event["kind"] == "needs_action"]
    assert len(failures) == 1
    event = failures[0]
    assert event["agent"] == "claude"
    assert event["from_source"] == source.id
    assert event["severity"] == "action_required"
    assert "Shared paid source" in event["human_en"]
    assert "Shared paid source" in event["human_zh"]
    _assert_valid("resolution-event.schema.json", event)

    agents = {
        item["backend"]: item
        for item in service.list_agents()
    }
    assert agents["claude"]["supply_status"] == "interrupted"
    assert agents["codex"]["supply_status"] == "interrupted"

    service.store.config.sources[0].state = ModelHubSourceStateConfig(
        status="standby"
    )
    recovered_agents = {
        item["backend"]: item
        for item in service.list_agents()
    }
    assert recovered_agents["claude"]["supply_status"] == "ok"
    assert recovered_agents["codex"]["supply_status"] == "ok"
    assert service.list_events(limit=20)[0] == event

    service.store.config.sources.clear()
    assert service.list_events(limit=20)[0] == event


def test_mapping_and_channel_switch_copy_retain_human_subjects() -> None:
    mapping = build_resolution_event(
        agent="codex",
        kind="mapping_applied",
        model_id="target-model",
        reason="mapping",
        from_label="requested-model",
        now=NOW,
    )
    channel_switch = build_resolution_event(
        agent="system",
        kind="channel_switch",
        model_id=None,
        reason="manual",
        from_source="src_primary01",
        to_source="src_primary01",
        from_label="Primary source",
        to_label="Primary source",
        now=NOW,
    )

    for text in (mapping.human_en, mapping.human_zh):
        assert "requested-model" in text
        assert "target-model" in text
    for text in (channel_switch.human_en, channel_switch.human_zh):
        assert "Primary source" in text


def test_shared_source_cooldown_emits_only_on_state_transition(
    tmp_path: Path,
) -> None:
    source = _source("src_primary01", "Shared source")
    service = _service(tmp_path, sources=[source])
    decision = classify_outcome(
        _outcome(
            RawOutcomeKind.HTTP_ERROR,
            status=429,
            code="rate_limited",
        )
    )

    async def cool_twice() -> None:
        await service._cooldown(
            source,
            decision,
            agent="claude",
            model_id="shared-model",
        )
        await service._cooldown(
            source,
            decision,
            agent="codex",
            model_id="shared-model",
        )

    asyncio.run(cool_twice())

    events = service.list_events(limit=20)
    assert len(events) == 1
    assert events[0]["kind"] == "cooldown"
    assert events[0]["agent"] == "claude"


def test_event_emission_rejects_unknown_source_ids(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        sources=[_source("src_primary01", "Primary")],
    )
    service._record_event(
        agent="claude",
        kind="switch",
        model_id="shared-model",
        reason="manual",
        from_source="src_unknown01",
        to_source="src_primary01",
        now=NOW,
    )
    service._record_event(
        agent="claude",
        kind="switch",
        model_id="shared-model",
        reason="manual",
        from_source="src_primary01",
        to_source="src_unknown02",
        now=NOW,
    )
    assert service.list_events(limit=20) == []


@pytest.mark.parametrize(
    "fields",
    [
        {
            "agent": "system",
            "kind": "supply_interrupted",
            "model_id": "shared-model",
            "reason": "no_enabled_source",
        },
        {
            "agent": "claude",
            "kind": "cooldown",
            "model_id": "shared-model",
            "reason": "credential_revoked",
            "from_source": "src_primary01",
        },
        {
            "agent": "claude",
            "kind": "needs_action",
            "model_id": "shared-model",
            "reason": "network",
            "from_source": "src_primary01",
        },
        {
            "agent": "claude",
            "kind": "switch",
            "model_id": "shared-model",
            "reason": "no_eligible_source",
            "from_source": "src_primary01",
            "to_source": "src_backup001",
        },
    ],
)
def test_event_builder_rejects_cross_grain_shapes(fields: dict) -> None:
    with pytest.raises(ValueError):
        build_resolution_event(**fields)


def test_provenance_absence_codes_are_distinguishable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path,
        sources=[_source("src_primary01", "Primary")],
    )
    monkeypatch.setattr(
        service,
        "_known_turn_backend",
        lambda turn_id: (
            "codex"
            if turn_id in {"turn_direct", "turn_ambiguous"}
            else None
        ),
    )

    service.store.config.agents["codex"].mode = "direct"
    with pytest.raises(ModelHubError) as direct:
        service.get_turn_provenance("turn_direct")
    assert direct.value.code == "provenance_unavailable"
    assert direct.value.detail == "models.provenance.direct_mode"

    service.store.config.agents["codex"].mode = "hub"
    with pytest.raises(ModelHubError) as ambiguous:
        service.get_turn_provenance("turn_ambiguous")
    assert ambiguous.value.code == "provenance_unavailable"
    assert (
        ambiguous.value.detail
        == "models.provenance.attribution_ambiguous"
    )

    with pytest.raises(ModelHubError) as unknown:
        service.get_turn_provenance("turn_unknown")
    assert unknown.value.code == "turn_not_found"
    assert unknown.value.status == 404
