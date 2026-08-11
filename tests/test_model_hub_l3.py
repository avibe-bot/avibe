from __future__ import annotations

import ast
import asyncio
import json
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import pytest
from aiohttp import web
from jsonschema import Draft7Validator, FormatChecker
from sqlalchemy import create_engine, delete, select

from config.v2_config import (
    ModelHubAgentSourcesConfig,
    ModelHubAgentSupplyConfig,
    ModelHubConfig,
    ModelHubModelConfig,
    ModelHubRouteConfig,
    ModelHubRouteHopConfig,
    ModelHubSourceConfig,
    ModelHubSourceStateConfig,
    model_hub_fixed_menu_ids,
)
from core.handlers.model_hub.adapter import (
    EngineHealth,
    EngineStatus,
    RawCallOutcome,
    RawOutcomeKind,
    SOURCE_PROTOCOLS,
)
from core.handlers.model_hub.classification import classify_outcome
from core.handlers.model_hub.events import (
    BoundedEventLog,
    EVENT_REASON_AUTHORITY,
    EventKind,
    EventReason,
    build_resolution_event,
    event_reason_label,
)
from core.handlers.model_hub.provenance import (
    BoundedProvenanceStore,
    ExactHopBlocker,
    SupplyState,
    TURN_OUTCOME_RENDERING_AUTHORITY,
    TurnOutcomeProjectionInput,
    TurnSupplyBlocker,
    TurnSupplyFacts,
    TurnCorrelationRegistry,
    project_turn_outcome_copy,
    render_turn_outcome_copy,
)
from core.handlers.model_hub.request import ModelHubRequest
from core.handlers.model_hub.revocations import CredentialRevocationJournal
from core.handlers.model_hub.service import ModelHubError, ModelHubService
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from core.run_settlement import (
    SETTLED_BY_NO_TERMINAL_RESULT,
    SETTLED_BY_STOPPED,
    SETTLED_BY_TERMINAL_RESULT,
)
from core.services.dispatch import TurnDispatchOutcome
from core.session_turns import SessionTurnManager
from modules.agents.model_hub import (
    ModelHubLaunch,
    ModelHubRuntimeRouter,
    _localized_launch_error,
    bind_launch,
    bind_turn_mode,
)
from storage.models import agent_sessions, messages, metadata
from vibe.i18n import t as i18n_t
from vibe.model_hub_runtime.adapter import (
    _AuthenticationEvidence,
    CLIProxyEngineAdapter,
    _parse_protocol_authenticated_evidence,
    _probe_protocol_response,
    _PROTOCOL_OBSERVATION_TAXONOMY,
    _ProtocolEvidence,
    _ProtocolProof,
)
from vibe.model_hub_runtime.client import EngineClientError, probe_models
from vibe.model_hub_runtime.state import EngineStateStore


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


def _turn_outcome_matrix() -> dict[str, str]:
    lines = (
        Path(__file__).parents[1] / "docs/plans/model-hub.md"
    ).read_text(encoding="utf-8").splitlines()
    heading = next(
        index
        for index, line in enumerate(lines)
        if line.startswith("**Turn-outcome copy matrix")
    )
    table = next(
        index
        for index in range(heading + 1, len(lines))
        if lines[index].startswith("| Decision |")
    )
    rows: dict[str, str] = {}
    for line in lines[table + 2 :]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        rows[cells[0].strip("`")] = cells[1].strip("`")
    return rows


def test_turn_outcome_rendering_authority_covers_matrix_and_locales() -> None:
    assert _turn_outcome_matrix() == {
        decision: rule.outcome
        for decision, rule in TURN_OUTCOME_RENDERING_AUTHORITY.items()
    }
    assert len(
        {
            (rule.outcome, rule.discriminator)
            for rule in TURN_OUTCOME_RENDERING_AUTHORITY.values()
        }
    ) == len(TURN_OUTCOME_RENDERING_AUTHORITY)

    projected_keys = {
        key
        for rule in TURN_OUTCOME_RENDERING_AUTHORITY.values()
        for _variant, key in rule.copy_keys
        if key is not None
    }
    locale_launch_keys = None
    for locale in ("en", "zh"):
        payload = json.loads(
            (Path(__file__).parents[1] / f"vibe/i18n/{locale}.json").read_text(
                encoding="utf-8"
            )
        )
        launch_keys = {
            f"modelHub.launch.{key}" for key in payload["modelHub"]["launch"]
        }
        locale_launch_keys = locale_launch_keys or launch_keys
        assert launch_keys == locale_launch_keys
        assert launch_keys == {
            key for key in projected_keys if key.startswith("modelHub.launch.")
        }
        assert all(i18n_t(key, locale) != key for key in projected_keys)


def test_turn_outcome_copy_projection_has_one_runtime_owner() -> None:
    root = Path(__file__).parents[1]
    owner = root / "core/handlers/model_hub/provenance.py"
    runtime_files = [
        *sorted((root / "core/handlers/model_hub").glob("*.py")),
        root / "modules/agents/model_hub.py",
    ]

    assert "modelHub.launch." in owner.read_text(encoding="utf-8")
    for path in runtime_files:
        if path == owner:
            continue
        source = path.read_text(encoding="utf-8")
        assert "modelHub.launch." not in source
        assert '"copy_key"' not in source


@pytest.mark.parametrize(
    ("decision", "variant", "expected_key"),
    tuple(
        (decision, variant, key)
        for decision, rule in TURN_OUTCOME_RENDERING_AUTHORITY.items()
        for variant, key in rule.copy_keys
    ),
)
def test_every_turn_outcome_matrix_variant_projects_or_stays_silent(
    decision: str,
    variant: str,
    expected_key: str | None,
) -> None:
    rule = TURN_OUTCOME_RENDERING_AUTHORITY[decision]
    supply_state = variant if variant in {"waiting", "interrupted"} else "waiting"
    projection = TurnOutcomeProjectionInput(
        outcome=rule.outcome,
        discriminator=rule.discriminator,
        supply_facts=TurnSupplyFacts(
            backend="claude",
            model="menu-model",
            supply_state=cast(SupplyState, supply_state),
            source="Exact source",
            retry_at=NOW.isoformat(),
            blockers=(TurnSupplyBlocker("Exact source", "model_unsupported"),),
        ),
        stream_started=variant == "stream_started",
        next_current_changed=variant == "next_current",
    )

    copy = project_turn_outcome_copy(projection)

    assert (copy.key if copy is not None else None) == expected_key
    rendered = render_turn_outcome_copy(projection, "en")
    assert (rendered is None) == (expected_key is None)


def test_route_unconfigured_launch_copy_exists_in_each_backend_locale() -> None:
    facts = TurnSupplyFacts(
        backend="claude",
        model="menu-model",
        supply_state="interrupted",
    )
    failure = ModelHubError(
        "no_candidate",
        turn_outcome=TurnOutcomeProjectionInput(
            outcome="no_candidate",
            discriminator="route_unconfigured",
            supply_facts=facts,
        ),
    )
    for locale in ("en", "zh"):
        rendered = _localized_launch_error(
            SimpleNamespace(config=SimpleNamespace(language=locale)),
            "claude",
            "menu-model",
            failure,
        )
        assert rendered.detail == i18n_t(
            "modelHub.launch.route_unconfigured",
            locale,
            model="menu-model",
        )


def test_structural_blocker_copy_uses_its_reason_instead_of_source_status() -> None:
    failure = ModelHubError(
        "no_candidate",
        turn_outcome=TurnOutcomeProjectionInput(
            outcome="no_candidate",
            discriminator="blocked_supply_state",
            supply_facts=TurnSupplyFacts(
                backend="claude",
                model="menu-model",
                supply_state="interrupted",
                blockers=(
                    TurnSupplyBlocker("Exact source", "model_unsupported"),
                ),
            ),
        ),
    )

    rendered = _localized_launch_error(
        SimpleNamespace(config=SimpleNamespace(language="en")),
        "claude",
        "menu-model",
        failure,
    )

    assert event_reason_label("model_unsupported", "en") in rendered.detail
    assert "standby" not in rendered.detail


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
        self.requests: list[ModelHubRequest] = []
        self.refreshable_credential_refs: set[str] = set()
        self.capability_queries: list[str] = []

    async def sync_sources(self, _bindings) -> None:
        return None

    async def revoke_credential(self, _credential_ref: str) -> None:
        return None

    async def credential_supports_refresh(self, credential_ref: str) -> bool:
        self.capability_queries.append(credential_ref)
        return credential_ref in self.refreshable_credential_refs

    async def invoke(self, source_id, model_id, request, _stream, origin):
        self.invocations.append((source_id, model_id, origin))
        self.requests.append(request)
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
    vendor: str = "openai",
    protocol: str = "openai_responses",
    model_id: str = "shared-model",
) -> ModelHubSourceConfig:
    return ModelHubSourceConfig(
        id=source_id,
        kind="subscription" if channel == "native_cli" else "api_key",
        vendor=vendor,
        display_name=label,
        protocol=protocol,
        supply_channel=channel,
        billing="monthly" if channel == "native_cli" else "metered",
        state=ModelHubSourceStateConfig(
            status=status,
            retry_at=retry_at,
            detail_key=("models.source.cooldown.rate_limited" if status == "cooldown" else None),
        ),
        models=[
            ModelHubModelConfig(
                id=model_id,
                provenance="discovered",
            )
        ],
        credential_ref=None if channel == "native_cli" else f"cred_{source_id}",
    )


def _config(sources: list[ModelHubSourceConfig]) -> ModelHubConfig:
    agents = {
        backend: ModelHubAgentSupplyConfig.default(backend, mode="hub") for backend in ("claude", "codex", "opencode")
    }
    for backend, agent in agents.items():
        eligible = [source for source in sources if ModelHubConfig.source_eligible_for_backend(source, backend)]
        agent.sources = ModelHubAgentSourcesConfig(
            order=[source.id for source in eligible],
        )
        requested_model = "openai/shared-model" if backend == "opencode" else "shared-model"
        agent.routes[requested_model] = ModelHubRouteConfig(
            hops=tuple(
                ModelHubRouteHopConfig(
                    source_id=source.id,
                    model_id=source.models[0].id,
                )
                for source in eligible
            )
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
    store = MemoryStore(_config(sources))
    return ModelHubService(
        store=store,
        adapter=ProbeAdapter(outcomes or []),
        events=BoundedEventLog(tmp_path / "events.json"),
        provenance=BoundedProvenanceStore(tmp_path / "provenance.json"),
        revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
        now=lambda: NOW,
        requested_model_override=store.requested_model,
    )


def _canonicalize_fixed_test_routes(
    service: ModelHubService,
) -> dict[str, str]:
    selected = {}
    config = service.store.load()
    for backend in ("claude", "codex"):
        menu_ids = model_hub_fixed_menu_ids(backend)
        selected[backend] = menu_ids[0]
        shared_route = config.agents[backend].routes.get(
            "shared-model",
            ModelHubRouteConfig(),
        )
        config.agents[backend].routes = {
            model_id: (shared_route if model_id == selected[backend] else ModelHubRouteConfig())
            for model_id in menu_ids
        }
        service.store.requested_models[backend] = selected[backend]
    return selected


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


def test_retired_process_scope_revokes_token_and_fails_closed(
    tmp_path: Path,
) -> None:
    store = BoundedProvenanceStore(tmp_path / "retired-scope.json")
    registry = TurnCorrelationRegistry(store)
    token = registry.credentials("codex", "/repo", "turn_evicted")
    turn_id = registry.begin_gateway_request(
        backend="codex",
        token=token,
        requested_model_id="shared-model",
    )
    registry.begin_attempt(
        turn_id,
        source_id="src_primary01",
        resolved_model_id="shared-model",
        channel="hub",
        via_mapping=False,
    )

    registry.retire_scope("codex", "/repo")

    assert registry.authenticates("codex", token) is False
    replacement = registry.credentials("codex", "/repo", "turn_replacement")
    assert replacement != token
    assert registry.authenticates("codex", replacement) is True
    registry.settle(
        "turn_evicted",
        settled_by=SETTLED_BY_NO_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )
    assert store.get("turn_evicted") is None

    registry.settle(
        "turn_replacement",
        settled_by=SETTLED_BY_NO_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )
    registry.retire_scope("codex", "/repo")
    assert registry._scopes == {}
    assert registry._token_scopes == {}


def test_terminal_failure_survives_exact_scope_retirement(
    tmp_path: Path,
) -> None:
    store = BoundedProvenanceStore(tmp_path / "terminal-retirement.json")
    registry = TurnCorrelationRegistry(store)
    registry.begin_native_attempt(
        backend="claude",
        process_scope="session:/repo",
        turn_id="turn_startup_failure",
        requested_model_id="claude-opus",
        source_id="src_native01",
        resolved_model_id="claude-opus",
        via_mapping=False,
    )
    registry.fail_native_attempt(
        "turn_startup_failure",
        reason="unclassified_error",
    )

    registry.retire_scope(
        "claude",
        "session:/repo",
        terminal_turn_id="turn_startup_failure",
    )
    registry.settle(
        "turn_startup_failure",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )

    record = store.get("turn_startup_failure")
    assert record is not None
    assert record["outcome"] == "exhausted"
    assert record["failed_attempts"][0]["reason"] == "unclassified_error"
    _assert_valid("turn-provenance.schema.json", record)


def test_terminal_retirement_does_not_restore_ambiguous_scope(
    tmp_path: Path,
) -> None:
    store = BoundedProvenanceStore(tmp_path / "ambiguous-retirement.json")
    registry = TurnCorrelationRegistry(store)
    registry.begin_native_attempt(
        backend="claude",
        process_scope="session:/repo",
        turn_id="turn_first",
        requested_model_id="claude-opus",
        source_id="src_native01",
        resolved_model_id="claude-opus",
        via_mapping=False,
    )
    registry.begin_native_attempt(
        backend="claude",
        process_scope="session:/repo",
        turn_id="turn_second",
        requested_model_id="claude-opus",
        source_id="src_native01",
        resolved_model_id="claude-opus",
        via_mapping=False,
    )
    registry.fail_native_attempt("turn_first", reason="unclassified_error")

    registry.retire_scope(
        "claude",
        "session:/repo",
        terminal_turn_id="turn_first",
    )
    registry.settle(
        "turn_first",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )

    assert store.get("turn_first") is None


@pytest.mark.parametrize(
    ("path", "body", "status", "reason"),
    [
        ("responses", "{", 400, "invalid_parameter"),
        ("responses", "[]", 400, "invalid_parameter"),
        ("responses", '{"stream":false}', 400, "invalid_parameter"),
        ("unsupported", '{"model":"shared-model"}', 404, "protocol_error"),
    ],
)
def test_authenticated_gateway_validation_failure_is_correlated(
    tmp_path: Path,
    path: str,
    body: str,
    status: int,
    reason: str,
) -> None:
    async def exercise() -> None:
        service = _service(
            tmp_path,
            sources=[_source("src_primary01", "Primary")],
        )
        gateway = ModelHubTurnGateway(service)
        base_url, token = await gateway.endpoint(
            "codex",
            process_scope="/repo",
            turn_id="turn_invalid_gateway_request",
            requested_model_id="shared-model",
            resolved_model_id="shared-model",
            source_id="src_primary01",
        )
        try:
            async with aiohttp.ClientSession(trust_env=False) as client:
                response = await client.post(
                    f"{base_url}/v1/{path}",
                    data=body,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
                assert response.status == status
                await response.read()
        finally:
            await gateway.close()

        gateway.correlation.settle(
            "turn_invalid_gateway_request",
            settled_by=SETTLED_BY_TERMINAL_RESULT,
            ts=NOW.isoformat(),
        )
        record = service.provenance.get("turn_invalid_gateway_request")
        assert record is not None
        assert record["outcome"] == "failed_terminal"
        assert record["terminal_error"] == {
            "source_id": "src_primary01",
            "configured_model_id": "shared-model",
            "channel": "hub",
            "reason": reason,
            "stream_started": False,
        }
        _assert_valid("turn-provenance.schema.json", record)

    asyncio.run(exercise())


def test_gateway_terminalizer_records_pre_observer_engine_down_before_return(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        service = _service(
            tmp_path,
            sources=[_source("src_primary01", "Primary")],
        )

        async def fail_sync(_bindings) -> None:
            raise RuntimeError("engine unavailable")

        service.adapter.sync_sources = fail_sync
        gateway = ModelHubTurnGateway(service)
        base_url, token = await gateway.endpoint(
            "codex",
            process_scope="/repo",
            turn_id="turn_pre_observer_engine_down",
            requested_model_id="shared-model",
            resolved_model_id="shared-model",
            source_id="src_primary01",
        )
        try:
            async with aiohttp.ClientSession(trust_env=False) as client:
                response = await client.post(
                    f"{base_url}/v1/responses",
                    json={
                        "model": "shared-model",
                        "input": "ping",
                        "stream": False,
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status == 503
                payload = await response.json()
                assert payload["error"]["message"] == i18n_t(
                    "modelHub.errors.engine_down",
                    "en",
                )

            trace = gateway.correlation._traces["turn_pre_observer_engine_down"]
            assert trace.pending_attempt is None
            assert trace.terminal_error == {
                "source_id": None,
                "configured_model_id": None,
                "channel": None,
                "reason": "engine_down",
                "stream_started": False,
            }
        finally:
            await gateway.close()

        gateway.correlation.settle(
            "turn_pre_observer_engine_down",
            settled_by=SETTLED_BY_TERMINAL_RESULT,
            ts=NOW.isoformat(),
        )
        record = service.provenance.get("turn_pre_observer_engine_down")
        assert record is not None
        assert record["outcome"] == "failed_terminal"
        assert record["terminal_error"] == {
            "source_id": None,
            "configured_model_id": None,
            "channel": None,
            "reason": "engine_down",
            "stream_started": False,
        }
        _assert_valid("turn-provenance.schema.json", record)

    asyncio.run(exercise())


@pytest.mark.parametrize("entry", ["process", "gateway"])
def test_no_candidate_provenance_carries_exact_hop_blockers_from_each_entry(
    tmp_path: Path,
    entry: str,
) -> None:
    turn_id = f"turn_blocked_{entry}"
    store = BoundedProvenanceStore(tmp_path / f"blocked-{entry}.json")
    registry = TurnCorrelationRegistry(store)
    blockers = (
        ExactHopBlocker(
            source_id="src_primary01",
            model_id="shared-model",
            reason="model_unsupported",
        ),
        ExactHopBlocker(
            source_id="src_backup001",
            model_id="shared-model",
            reason="credential_revoked",
        ),
    )
    if entry == "process":
        registry.mark_no_candidate(
            backend="claude",
            process_scope="/repo",
            turn_id=turn_id,
            requested_model_id="shared-model",
            supply_state="interrupted",
            blockers=blockers,
        )
    else:
        token = registry.credentials("claude", "/repo", turn_id)
        registry.prepare_gateway_turn(
            backend="claude",
            token=token,
            requested_model_id="shared-model",
            resolved_model_id="shared-model",
            source_id="src_primary01",
            via_mapping=False,
        )
        with registry.gateway_terminalizer(
            backend="claude",
            token=token,
        ) as terminalizer:
            terminalizer.mark_no_candidate("interrupted", blockers)

    registry.settle(
        turn_id,
        settled_by=SETTLED_BY_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )

    record = store.get(turn_id)
    assert record is not None
    assert record["outcome"] == "no_candidate"
    assert record["blockers"] == [
        {
            "source_id": "src_primary01",
            "model_id": "shared-model",
            "reason": "model_unsupported",
        },
        {
            "source_id": "src_backup001",
            "model_id": "shared-model",
            "reason": "credential_revoked",
        },
    ]
    _assert_valid("turn-provenance.schema.json", record)


def test_runtime_no_candidate_projects_live_exact_hop_blockers(
    tmp_path: Path,
) -> None:
    source = _source("src_blocked01", "Blocked")
    source.state = ModelHubSourceStateConfig(
        status="needs_action",
        detail_key="models.source.needs_action.credential_revoked",
    )
    service = _service(tmp_path, sources=[source])
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(
            router.resolve(
                "claude",
                "shared-model",
                process_scope="/repo",
                turn_id="turn_runtime_blocked",
            )
        )

    assert exc_info.value.supply_state == "interrupted"
    router.settle_turn(
        "turn_runtime_blocked",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )
    record = service.provenance.get("turn_runtime_blocked")
    assert record is not None
    assert record["outcome"] == "no_candidate"
    assert record["blockers"] == [
        {
            "source_id": source.id,
            "model_id": "shared-model",
            "reason": "credential_revoked",
        }
    ]
    _assert_valid("turn-provenance.schema.json", record)


def test_gateway_no_candidate_projects_live_exact_hop_blockers(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        source = _source("src_blocked02", "Blocked")
        source.state = ModelHubSourceStateConfig(
            status="needs_action",
            detail_key="models.source.needs_action.credential_revoked",
        )
        service = _service(tmp_path, sources=[source])
        service.store.config.agents["codex"].routes["shared-model"] = ModelHubRouteConfig(
            hops=(ModelHubRouteHopConfig(source.id, "removed-model"),)
        )
        gateway = ModelHubTurnGateway(service)
        base_url, token = await gateway.endpoint(
            "codex",
            process_scope="/repo",
            turn_id="turn_gateway_blocked",
            requested_model_id="shared-model",
            resolved_model_id="removed-model",
            source_id=source.id,
        )
        try:
            async with aiohttp.ClientSession(trust_env=False) as client:
                response = await client.post(
                    f"{base_url}/v1/responses",
                    json={"model": "removed-model", "input": "ping", "stream": False},
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status == 409
                await response.read()
        finally:
            await gateway.close()

        gateway.correlation.settle(
            "turn_gateway_blocked",
            settled_by=SETTLED_BY_TERMINAL_RESULT,
            ts=NOW.isoformat(),
        )
        record = service.provenance.get("turn_gateway_blocked")
        assert record is not None
        assert record["outcome"] == "no_candidate"
        assert record["blockers"] == [
            {
                "source_id": source.id,
                "model_id": "removed-model",
                "reason": "model_unsupported",
            }
        ]
        _assert_valid("turn-provenance.schema.json", record)

    asyncio.run(exercise())


def test_gateway_preserves_exhausted_provenance_after_all_hops_fallback(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        first = _source("src_primary01", "Primary")
        second = _source("src_backup001", "Backup")
        service = _service(
            tmp_path,
            sources=[first, second],
            outcomes=[
                _outcome(
                    RawOutcomeKind.HTTP_ERROR,
                    status=429,
                    source_id=first.id,
                ),
                _outcome(
                    RawOutcomeKind.HTTP_ERROR,
                    status=503,
                    source_id=second.id,
                ),
            ],
        )
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        service.store.config.agents["codex"].routes[requested_model] = ModelHubRouteConfig(
            hops=(
                ModelHubRouteHopConfig(first.id, "shared-model"),
                ModelHubRouteHopConfig(second.id, "shared-model"),
            )
        )
        gateway = ModelHubTurnGateway(service)
        base_url, token = await gateway.endpoint(
            "codex",
            process_scope="/repo",
            turn_id="turn_gateway_exhausted",
            requested_model_id=requested_model,
            resolved_model_id="shared-model",
            source_id=first.id,
        )
        try:
            async with aiohttp.ClientSession(trust_env=False) as client:
                response = await client.post(
                    f"{base_url}/v1/responses",
                    json={"model": "shared-model", "input": "ping", "stream": False},
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status == 503
                payload = await response.json()
                assert payload["error"]["code"] == "mapping_target_unavailable"
                cooled = service.store.load().sources
                assert payload["error"]["message"] == i18n_t(
                    "modelHub.launch.waiting",
                    "en",
                    model=requested_model,
                    source=", ".join(source.display_name for source in cooled),
                    retry_at=min(source.state.retry_at or "" for source in cooled),
                )
        finally:
            await gateway.close()

        gateway.correlation.settle(
            "turn_gateway_exhausted",
            settled_by=SETTLED_BY_TERMINAL_RESULT,
            ts=NOW.isoformat(),
        )
        record = service.provenance.get("turn_gateway_exhausted")
        assert record is not None
        assert record["outcome"] == "exhausted"
        assert record["terminal_error"] is None
        assert [attempt["source_id"] for attempt in record["failed_attempts"]] == [
            first.id,
            second.id,
        ]
        _assert_valid("turn-provenance.schema.json", record)

    asyncio.run(exercise())


def test_gateway_localizes_terminal_permission_denial_without_switching(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        source = _source("src_primary01", "Primary")
        service = _service(
            tmp_path,
            sources=[source],
            outcomes=[
                _outcome(
                    RawOutcomeKind.HTTP_ERROR,
                    status=403,
                    code="permission_error",
                    source_id=source.id,
                )
            ],
        )
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service, language_provider=lambda: "zh")
        base_url, token = await gateway.endpoint(
            "codex",
            process_scope="/repo",
            turn_id="turn_permission_denied",
            requested_model_id=requested_model,
            resolved_model_id="shared-model",
            source_id=source.id,
        )
        try:
            async with aiohttp.ClientSession(trust_env=False) as client:
                response = await client.post(
                    f"{base_url}/v1/responses",
                    json={"model": "shared-model", "input": "ping", "stream": False},
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status == 403
                payload = await response.json()
                assert payload["error"] == {
                    "type": "request_incompatible",
                    "code": "request_incompatible",
                    "message": i18n_t("modelHub.launch.request_incompatible", "zh"),
                }
        finally:
            await gateway.close()

        assert service.adapter.invocations == [
            (source.id, "shared-model", "codex")
        ]
        assert service.store.load().sources[0].state.status == "standby"
        gateway.correlation.settle(
            "turn_permission_denied",
            settled_by=SETTLED_BY_TERMINAL_RESULT,
            ts=NOW.isoformat(),
        )
        record = service.provenance.get("turn_permission_denied")
        assert record is not None
        assert record["outcome"] == "failed_terminal"
        assert record["failed_attempts"] == []
        assert record["terminal_error"] == {
            "source_id": source.id,
            "configured_model_id": "shared-model",
            "channel": "hub",
            "reason": "invalid_parameter",
            "stream_started": False,
        }
        _assert_valid("turn-provenance.schema.json", record)

    asyncio.run(exercise())


def test_gateway_provenance_retains_pre_mapping_model_identity(
    tmp_path: Path,
) -> None:
    store = BoundedProvenanceStore(tmp_path / "provenance.json")
    registry = TurnCorrelationRegistry(store)
    token = registry.credentials("codex", "/repo", "turn_mapped")
    registry.prepare_gateway_turn(
        backend="codex",
        token=token,
        requested_model_id="gpt-5",
        resolved_model_id="custom-gpt-5",
        source_id="src_primary01",
        via_mapping=True,
    )

    exact_turn = registry.begin_gateway_request(
        backend="codex",
        token=token,
        requested_model_id="custom-gpt-5",
    )
    registry.begin_attempt(
        exact_turn,
        source_id="src_primary01",
        resolved_model_id="custom-gpt-5",
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
        "turn_mapped",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )

    record = store.get("turn_mapped")
    assert record is not None
    assert record["requested_model_id"] == "gpt-5"
    assert record["served"] == {
        "source_id": "src_primary01",
        "configured_model_id": "custom-gpt-5",
        "channel": "hub",
    }
    _assert_valid("turn-provenance.schema.json", record)


def test_gateway_uses_persisted_exact_hops_for_failover(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        requested_model = "claude-opus-4-5"
        primary_model = "claude-opus-4-5-20251101"
        backup_model = "claude-opus-4-5-20250929"
        primary = _source(
            "src_primary01",
            "Primary",
            vendor="anthropic",
            protocol="anthropic",
            model_id=primary_model,
        )
        backup = _source(
            "src_backup001",
            "Backup",
            vendor="anthropic",
            protocol="anthropic",
            model_id=backup_model,
        )
        service = _service(
            tmp_path,
            sources=[primary, backup],
            outcomes=[
                _outcome(
                    RawOutcomeKind.HTTP_ERROR,
                    status=429,
                    source_id=primary.id,
                ),
                _outcome(
                    RawOutcomeKind.SUCCESS,
                    source_id=backup.id,
                ),
            ],
        )
        _canonicalize_fixed_test_routes(service)
        service.store.config.agents["claude"].routes[requested_model] = ModelHubRouteConfig(
            hops=(
                ModelHubRouteHopConfig(primary.id, primary_model),
                ModelHubRouteHopConfig(backup.id, backup_model),
            )
        )
        gateway = ModelHubTurnGateway(service)
        base_url, token = await gateway.endpoint(
            "claude",
            process_scope="/repo",
            turn_id="turn_native_alias",
            requested_model_id=requested_model,
            resolved_model_id=primary_model,
            source_id=primary.id,
        )
        try:
            async with aiohttp.ClientSession(trust_env=False) as client:
                response = await client.post(
                    f"{base_url}/v1/messages",
                    json={
                        "model": primary_model,
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "ping"}],
                        "stream": False,
                    },
                    headers={"x-api-key": token},
                )
                assert response.status == 200
                await response.read()
        finally:
            await gateway.close()

        assert service.adapter.invocations == [
            (primary.id, primary_model, "claude"),
            (backup.id, backup_model, "claude"),
        ]
        failover_events = [event for event in service.events.list(limit=20) if event["kind"] in {"cooldown", "switch"}]
        assert {event["model_id"] for event in failover_events} == {requested_model}

        gateway.correlation.settle(
            "turn_native_alias",
            settled_by=SETTLED_BY_TERMINAL_RESULT,
            ts=NOW.isoformat(),
        )
        record = service.provenance.get("turn_native_alias")
        assert record is not None
        assert record["requested_model_id"] == requested_model
        assert record["served"]["source_id"] == backup.id
        assert record["served"]["configured_model_id"] == backup_model
        _assert_valid("turn-provenance.schema.json", record)

    asyncio.run(exercise())


def test_gateway_model_outside_prepared_turn_fails_closed(
    tmp_path: Path,
) -> None:
    store = BoundedProvenanceStore(tmp_path / "provenance.json")
    registry = TurnCorrelationRegistry(store)
    token = registry.credentials("codex", "/repo", "turn_expected")
    registry.prepare_gateway_turn(
        backend="codex",
        token=token,
        requested_model_id="gpt-5",
        resolved_model_id="custom-gpt-5",
        source_id="src_primary01",
        via_mapping=True,
    )

    assert (
        registry.begin_gateway_request(
            backend="codex",
            token=token,
            requested_model_id="untracked-model",
        )
        is None
    )
    registry.settle(
        "turn_expected",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )
    assert store.get("turn_expected") is None


def test_native_terminal_failure_is_not_recorded_as_served(
    tmp_path: Path,
) -> None:
    store = BoundedProvenanceStore(tmp_path / "native-failure.json")
    registry = TurnCorrelationRegistry(store)
    registry.begin_native_attempt(
        backend="codex",
        process_scope="/repo",
        turn_id="turn_native_failure",
        requested_model_id="shared-model",
        source_id="src_primary01",
        resolved_model_id="shared-model",
        via_mapping=False,
    )

    registry.fail_native_attempt(
        "turn_native_failure",
        reason="quota_exhausted",
    )
    registry.settle(
        "turn_native_failure",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )

    record = store.get("turn_native_failure")
    assert record is not None
    assert record["outcome"] == "exhausted"
    assert record["served"] is None
    assert record["failed_attempts"] == [
        {
            "source_id": "src_primary01",
            "configured_model_id": "shared-model",
            "channel": "native_cli",
            "reason": "quota_exhausted",
        }
    ]
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


def test_backend_terminal_failure_overrides_gateway_success(
    tmp_path: Path,
) -> None:
    store = BoundedProvenanceStore(tmp_path / "backend-terminal.json")
    registry = TurnCorrelationRegistry(store)
    exact_turn = _begin_hub_attempt(
        registry,
        turn_id="turn_backend_terminal",
    )
    success = _outcome(RawOutcomeKind.SUCCESS)
    registry.finish_attempt(
        exact_turn,
        outcome=success,
        decision=classify_outcome(success),
    )

    registry.fail_hub_attempt("turn_backend_terminal")
    registry.settle(
        "turn_backend_terminal",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )

    record = store.get("turn_backend_terminal")
    assert record is not None
    assert record["outcome"] == "failed_terminal"
    assert record["served"] is None
    assert record["terminal_error"]["reason"] == "protocol_error"
    _assert_valid("turn-provenance.schema.json", record)


def test_lost_terminal_retains_completed_fallback_attempts(
    tmp_path: Path,
) -> None:
    store = BoundedProvenanceStore(tmp_path / "lost-terminal.json")
    registry = TurnCorrelationRegistry(store)
    exact_turn = _begin_hub_attempt(
        registry,
        turn_id="turn_lost_terminal",
    )
    failure = _outcome(
        RawOutcomeKind.HTTP_ERROR,
        status=429,
    )
    registry.finish_attempt(
        exact_turn,
        outcome=failure,
        decision=classify_outcome(failure),
    )

    registry.settle(
        "turn_lost_terminal",
        settled_by=SETTLED_BY_NO_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )

    record = store.get("turn_lost_terminal")
    assert record is not None
    assert record["outcome"] == "exhausted"
    assert record["failed_attempts"][0]["reason"] == "rate_limited"
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
            settle_turn=lambda value, *, settled_by, ts, mode=None: registry.settle(
                value,
                settled_by=settled_by,
                ts=ts,
            )
        )
        controller = SimpleNamespace(
            model_hub_runtime=runtime,
            command_handler=SimpleNamespace(handle_stop=AsyncMock(return_value=True)),
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


def test_fsm_settlement_persists_the_turn_time_mode() -> None:
    runtime = SimpleNamespace(settle_turn=Mock())
    manager = SessionTurnManager(SimpleNamespace(model_hub_runtime=runtime))
    context = SimpleNamespace(platform_specific={"turn_token": "turn_direct"})
    bind_launch(
        context,
        ModelHubLaunch(
            backend="codex",
            channel="direct",
            requested_model="gpt-5",
            target_model="gpt-5",
            runtime_model="gpt-5",
        ),
    )

    manager._settle_model_hub_turn(
        context,
        SETTLED_BY_TERMINAL_RESULT,
    )

    runtime.settle_turn.assert_called_once()
    assert runtime.settle_turn.call_args.kwargs["mode"] == "direct"


def test_fsm_settlement_persists_model_less_direct_mode() -> None:
    runtime = SimpleNamespace(settle_turn=Mock())
    manager = SessionTurnManager(SimpleNamespace(model_hub_runtime=runtime))
    context = SimpleNamespace(platform_specific={"turn_token": "turn_opencode_default"})
    bind_turn_mode(context, "direct")

    manager._settle_model_hub_turn(
        context,
        SETTLED_BY_TERMINAL_RESULT,
    )

    runtime.settle_turn.assert_called_once()
    assert runtime.settle_turn.call_args.kwargs["mode"] == "direct"


def test_settlement_retires_correlation_when_mode_persistence_fails(
    tmp_path: Path,
) -> None:
    correlation = Mock()
    service = _service(
        tmp_path,
        sources=[_source("src_primary01", "Primary")],
    )
    service.note_turn_mode = Mock(side_effect=OSError("mode store unavailable"))
    router = ModelHubRuntimeRouter(
        service=service,
        turn_gateway=SimpleNamespace(correlation=correlation),
    )

    with pytest.raises(OSError, match="mode store unavailable"):
        router.settle_turn(
            "turn_mode_failure",
            settled_by=SETTLED_BY_TERMINAL_RESULT,
            ts=NOW.isoformat(),
            mode="hub",
        )

    correlation.settle.assert_called_once_with(
        "turn_mode_failure",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )


def test_opencode_overlay_projects_menu_identity_to_exact_hop_model(tmp_path: Path) -> None:
    source = _source("src_overlay01", "Overlay", model_id="upstream-model")
    config = _config([source])
    agent = config.agents["opencode"]
    agent.routes.pop("openai/shared-model")
    agent.routes["openai/menu-model"] = ModelHubRouteConfig(hops=(ModelHubRouteHopConfig(source.id, "upstream-model"),))
    agent.menu.checked = ["openai/menu-model"]
    service = _service(tmp_path, sources=[source])
    service.store.config = config
    gateway = SimpleNamespace(
        endpoint=AsyncMock(return_value=("http://127.0.0.1:19000", "gateway-token")),
    )
    router = ModelHubRuntimeRouter(
        service=service,
        turn_gateway=gateway,
        overlay_path=tmp_path / "overlay.json",
    )

    overlay = asyncio.run(router.prepare_opencode_overlay())

    assert overlay is not None
    payload = json.loads(overlay.content)
    assert payload["provider"]["openai"]["models"]["menu-model"]["id"] == ("openai/menu-model")
    assert overlay.launches[0].target_model == "upstream-model"


def test_opencode_overlay_supports_mixed_protocols_under_one_provider(tmp_path: Path) -> None:
    first = _source(
        "src_overlay11",
        "First",
        vendor="custom",
        protocol="anthropic",
        model_id="first-model",
    )
    second = _source(
        "src_overlay12",
        "Second",
        vendor="custom",
        protocol="openai_responses",
        model_id="second-model",
    )
    config = _config([first, second])
    agent = config.agents["opencode"]
    agent.menu.checked = ["custom/first-model", "custom/second-model"]
    agent.routes = {
        "custom/first-model": ModelHubRouteConfig(hops=(ModelHubRouteHopConfig(first.id, "first-model"),)),
        "custom/second-model": ModelHubRouteConfig(hops=(ModelHubRouteHopConfig(second.id, "second-model"),)),
    }
    service = _service(tmp_path, sources=[first, second])
    service.store.config = config
    router = ModelHubRuntimeRouter(
        service=service,
        turn_gateway=SimpleNamespace(
            endpoint=AsyncMock(return_value=("http://127.0.0.1:19000/opencode", "gateway-token")),
        ),
        overlay_path=tmp_path / "overlay.json",
    )

    overlay = asyncio.run(router.prepare_opencode_overlay())

    assert overlay is not None
    provider = json.loads(overlay.content)["provider"]["custom"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"] == "http://127.0.0.1:19000/opencode/v1"
    assert set(provider["models"]) == {"first-model", "second-model"}
    assert {launch.target_model for launch in overlay.launches} == {
        "first-model",
        "second-model",
    }


def test_opencode_overlay_selects_supported_fallback_by_exact_hop(tmp_path: Path) -> None:
    source = _source(
        "src_overlay03",
        "Overlay",
        model_id="supported-model",
    )
    config = _config([source])
    agent = config.agents["opencode"]
    agent.routes.pop("openai/shared-model")
    agent.routes["openai/menu-model"] = ModelHubRouteConfig(
        hops=(
            ModelHubRouteHopConfig(source.id, "stale-model"),
            ModelHubRouteHopConfig(source.id, "supported-model"),
        )
    )
    agent.menu.checked = ["openai/menu-model"]
    service = _service(tmp_path, sources=[source])
    service.store.config = config
    router = ModelHubRuntimeRouter(
        service=service,
        turn_gateway=SimpleNamespace(
            endpoint=AsyncMock(return_value=("http://127.0.0.1:19000", "gateway-token")),
        ),
        overlay_path=tmp_path / "overlay.json",
    )

    overlay = asyncio.run(router.prepare_opencode_overlay())

    assert overlay is not None
    assert [launch.target_model for launch in overlay.launches] == ["supported-model"]


def test_opencode_overlay_preserves_checked_route_with_stale_exact_hop(
    tmp_path: Path,
) -> None:
    source = _source(
        "src_overlay04",
        "Overlay",
        model_id="current-model",
    )
    config = _config([source])
    agent = config.agents["opencode"]
    agent.routes.pop("openai/shared-model")
    agent.routes["openai/menu-model"] = ModelHubRouteConfig(hops=(ModelHubRouteHopConfig(source.id, "stale-model"),))
    agent.menu.checked = ["openai/menu-model"]
    service = _service(tmp_path, sources=[source])
    service.store.config = config
    router = ModelHubRuntimeRouter(
        service=service,
        turn_gateway=SimpleNamespace(
            endpoint=AsyncMock(return_value=("http://127.0.0.1:19000", "gateway-token")),
        ),
        overlay_path=tmp_path / "overlay.json",
    )

    overlay = asyncio.run(router.prepare_opencode_overlay())

    assert overlay is not None
    provider = json.loads(overlay.content)["provider"]["openai"]
    assert provider["models"] == {
        "menu-model": {
            "id": "openai/menu-model",
            "name": "menu-model",
        }
    }
    assert overlay.checked_identifiers == ("openai/menu-model",)
    assert overlay.available_identifiers == ()
    assert overlay.launches == ()


def test_production_adapter_retargets_api_keys_without_exposing_or_mutating_them(
    tmp_path: Path,
) -> None:
    state_store = EngineStateStore(tmp_path / "engine-state")
    old_ref = state_store.store_api_key(
        "test-retarget-secret",
        vendor="custom",
        protocol="openai_chat",
        base_url="https://old-relay.example/v1",
    )
    oauth_ref = state_store.bind_oauth_credential(
        "src_oauthrefresh",
        "openai",
        "codex-test.json",
    )
    adapter = CLIProxyEngineAdapter(
        supervisor=Mock(),
        state_store=state_store,
    )

    replacement_ref = asyncio.run(
        adapter.retarget_api_key_credential(
            old_ref,
            "custom",
            "openai_chat",
            "https://new-relay.example/v1?api-version=2026-08-10",
        )
    )

    assert replacement_ref != old_ref
    assert state_store.credential_metadata(old_ref)["base_url"] == ("https://old-relay.example/v1")
    assert state_store.credential_metadata(replacement_ref)["base_url"] == (
        "https://new-relay.example/v1?api-version=2026-08-10"
    )
    assert state_store.read_api_key(old_ref) == "test-retarget-secret"
    assert state_store.read_api_key(replacement_ref) == "test-retarget-secret"
    assert asyncio.run(adapter.credential_supports_refresh(old_ref)) is False
    assert asyncio.run(adapter.credential_supports_refresh(oauth_ref)) is True


def test_source_observation_reduces_the_order_at_the_first_authenticated_proof(
    tmp_path: Path,
) -> None:
    base_url = "https://relay.example/v1?api-version=2026-07-23"
    state_store = EngineStateStore(tmp_path / "engine-state")
    credential_ref = state_store.store_api_key(
        "test-observation-key",
        vendor="openai",
        protocol=SOURCE_PROTOCOLS[-1],
        base_url=base_url,
    )
    adapter = CLIProxyEngineAdapter(
        supervisor=Mock(),
        state_store=state_store,
    )
    unsupported = EngineClientError(
        "unsupported protocol path",
        status_code=404,
    )
    proven_accepted = _ProtocolEvidence(
        protocol=_ProtocolProof.PROVEN,
        authentication=_AuthenticationEvidence.ACCEPTED,
    )
    proven_rejected = _ProtocolEvidence(
        protocol=_ProtocolProof.PROVEN,
        authentication=_AuthenticationEvidence.REJECTED,
    )
    proven_unknown = _ProtocolEvidence(
        protocol=_ProtocolProof.PROVEN,
        authentication=_AuthenticationEvidence.UNKNOWN,
    )
    unproven_unknown = _ProtocolEvidence(
        protocol=_ProtocolProof.UNPROVEN,
        authentication=_AuthenticationEvidence.UNKNOWN,
    )

    hinted_order = tuple(reversed(SOURCE_PROTOCOLS))

    async def every_candidate_is_supported(**_kwargs) -> _ProtocolEvidence:
        return proven_accepted

    with (
        patch(
            "vibe.model_hub_runtime.adapter._probe_protocol_response",
            new=AsyncMock(side_effect=every_candidate_is_supported),
        ) as protocol_probe,
        patch(
            "vibe.model_hub_runtime.adapter.probe_models",
            new=AsyncMock(return_value=("upstream-model",)),
        ) as inventory_probe,
    ):
        observed = asyncio.run(
            adapter.observe_source(
                "openai",
                base_url,
                credential_ref,
                hinted_order,
            )
        )

    assert observed.outcome.value == "observed"
    assert observed.protocol == hinted_order[0]
    assert [call.kwargs["protocol"] for call in protocol_probe.await_args_list] == [hinted_order[0]]
    assert inventory_probe.await_args.kwargs["protocol"] == hinted_order[0]

    async def indistinguishable_response(**_kwargs) -> _ProtocolEvidence:
        return unproven_unknown

    with (
        patch(
            "vibe.model_hub_runtime.adapter._probe_protocol_response",
            new=AsyncMock(side_effect=indistinguishable_response),
        ) as protocol_probe,
        patch(
            "vibe.model_hub_runtime.adapter.probe_models",
            new=AsyncMock(return_value=("upstream-model",)),
        ) as inventory_probe,
    ):
        ambiguous = asyncio.run(
            adapter.observe_source(
                "openai",
                base_url,
                credential_ref,
                hinted_order,
            )
        )

    assert ambiguous.outcome.value == "ambiguous"
    assert ambiguous.protocol is None
    assert ambiguous.authenticated is None
    assert [call.kwargs["protocol"] for call in protocol_probe.await_args_list] == list(hinted_order)
    inventory_probe.assert_not_awaited()

    async def shaped_server_failure(**kwargs) -> _ProtocolEvidence:
        if kwargs["protocol"] == "anthropic":
            body = {"type": "error", "error": {"type": "api_error"}}
        else:
            body = {"error": {"type": "server_error"}}
        evidence = _parse_protocol_authenticated_evidence(
            kwargs["protocol"],
            500,
            json.dumps(body),
        )
        assert evidence == proven_unknown
        return evidence

    with (
        patch(
            "vibe.model_hub_runtime.adapter._probe_protocol_response",
            new=AsyncMock(side_effect=shaped_server_failure),
        ) as protocol_probe,
        patch(
            "vibe.model_hub_runtime.adapter.probe_models",
            new=AsyncMock(return_value=("unverified-model",)),
        ) as inventory_probe,
    ):
        upstream_error = asyncio.run(
            adapter.observe_source(
                "openai",
                base_url,
                credential_ref,
                SOURCE_PROTOCOLS,
            )
        )

    assert upstream_error.outcome.value == "adapter_error"
    assert upstream_error.reachable is True
    assert upstream_error.authenticated is None
    assert upstream_error.protocol is None
    assert [call.kwargs["protocol"] for call in protocol_probe.await_args_list] == list(SOURCE_PROTOCOLS)
    inventory_probe.assert_not_awaited()
    _assert_valid(
        "observation-result.schema.json",
        {
            "contract_version": 5,
            "outcome": "adapter_error",
            "reachable": True,
            "authenticated": "unknown",
            "protocol": None,
            "discovery": "not_attempted",
            "models": [],
        },
    )

    proved_protocol = SOURCE_PROTOCOLS[1]

    async def later_protocol_probe(**kwargs) -> _ProtocolEvidence:
        if kwargs["protocol"] != proved_protocol:
            raise unsupported
        return proven_accepted

    with (
        patch(
            "vibe.model_hub_runtime.adapter._probe_protocol_response",
            new=AsyncMock(side_effect=later_protocol_probe),
        ) as protocol_probe,
        patch(
            "vibe.model_hub_runtime.adapter.probe_models",
            new=AsyncMock(return_value=("upstream-model",)),
        ) as inventory_probe,
    ):
        observed = asyncio.run(
            adapter.observe_source(
                "openai",
                base_url,
                credential_ref,
                SOURCE_PROTOCOLS,
            )
        )

    assert observed.outcome.value == "observed"
    assert observed.protocol == proved_protocol
    assert observed.model_ids == ("upstream-model",)
    assert [call.kwargs["protocol"] for call in protocol_probe.await_args_list] == list(SOURCE_PROTOCOLS[:2])
    assert inventory_probe.await_args.kwargs["protocol"] == proved_protocol
    assert inventory_probe.await_args.kwargs["base_url"] == base_url

    rejected_protocol = SOURCE_PROTOCOLS[0]
    accepted_protocol = SOURCE_PROTOCOLS[1]

    async def rejected_then_authenticated(**kwargs) -> _ProtocolEvidence:
        if kwargs["protocol"] == rejected_protocol:
            return proven_rejected
        return proven_accepted

    with (
        patch(
            "vibe.model_hub_runtime.adapter._probe_protocol_response",
            new=AsyncMock(side_effect=rejected_then_authenticated),
        ) as protocol_probe,
        patch(
            "vibe.model_hub_runtime.adapter.probe_models",
            new=AsyncMock(return_value=("later-protocol-model",)),
        ) as inventory_probe,
    ):
        observed = asyncio.run(
            adapter.observe_source(
                "openai",
                base_url,
                credential_ref,
                SOURCE_PROTOCOLS,
            )
        )

    assert observed.outcome.value == "observed"
    assert observed.authenticated is True
    assert observed.protocol == accepted_protocol
    assert observed.model_ids == ("later-protocol-model",)
    assert [call.kwargs["protocol"] for call in protocol_probe.await_args_list] == [
        rejected_protocol,
        accepted_protocol,
    ]
    assert inventory_probe.await_args.kwargs["protocol"] == accepted_protocol

    async def reject_every_candidate(**kwargs) -> _ProtocolEvidence:
        return _parse_protocol_authenticated_evidence(
            kwargs["protocol"],
            401,
            json.dumps(
                {
                    "error": {
                        "type": "invalid_request_error",
                        "code": "invalid_api_key",
                        "param": None,
                    }
                }
            ),
        )

    with (
        patch(
            "vibe.model_hub_runtime.adapter._probe_protocol_response",
            new=AsyncMock(side_effect=reject_every_candidate),
        ) as protocol_probe,
        patch(
            "vibe.model_hub_runtime.adapter.probe_models",
            new=AsyncMock(return_value=("unreachable-model",)),
        ) as inventory_probe,
    ):
        rejected = asyncio.run(
            adapter.observe_source(
                "openai",
                base_url,
                credential_ref,
                SOURCE_PROTOCOLS,
            )
        )

    assert rejected.outcome.value == "authentication_failed"
    assert rejected.reachable is True
    assert rejected.authenticated is False
    assert rejected.protocol is None
    assert [call.kwargs["protocol"] for call in protocol_probe.await_args_list] == list(SOURCE_PROTOCOLS)
    inventory_probe.assert_not_awaited()


def test_oauth_observation_uses_the_bound_auth_index_and_requires_response_proof(
    tmp_path: Path,
) -> None:
    state_store = EngineStateStore(tmp_path / "engine-state")
    credential_ref = state_store.bind_oauth_credential(
        "src_oauthproof",
        "openai",
        "codex-test.json",
    )
    client = Mock()
    api_calls: list[dict] = []

    def management_request(method, path, *, query=None, payload=None):
        if path == "/auth-files":
            return {
                "files": [
                    {
                        "id": "codex-test",
                        "auth_index": "auth-index-test",
                        "name": "codex-test.json",
                        "provider": "codex",
                        "id_token": {"chatgpt_account_id": "account-test"},
                    }
                ]
            }
        if path == "/api-call":
            api_calls.append(payload)
            return {
                "status_code": 400,
                "header": {},
                "body": json.dumps(
                    {
                        "error": {
                            "type": "invalid_request_error",
                            "param": "input",
                        }
                    }
                ),
            }
        if path == "/auth-files/models":
            assert query == {"name": "codex-test.json"}
            return {"models": [{"id": "gpt-5.6"}]}
        raise AssertionError((method, path))

    client.management_request.side_effect = management_request
    supervisor = Mock()
    supervisor.client.return_value = client
    adapter = CLIProxyEngineAdapter(
        supervisor=supervisor,
        state_store=state_store,
    )

    observed = asyncio.run(
        adapter.observe_source(
            "openai",
            None,
            credential_ref,
            SOURCE_PROTOCOLS,
        )
    )

    assert observed.outcome.value == "observed"
    assert observed.protocol == "openai_responses"
    assert observed.model_ids == ("gpt-5.6",)
    assert len(api_calls) == 1
    assert api_calls[0]["auth_index"] == "auth-index-test"
    assert api_calls[0]["header"]["Chatgpt-Account-Id"] == "account-test"
    assert api_calls[0]["url"].endswith("/responses")
    assert json.loads(api_calls[0]["data"]) == dict(
        _PROTOCOL_OBSERVATION_TAXONOMY["openai_responses"].request_body
    )

    def ambiguous_management_request(method, path, *, query=None, payload=None):
        if path == "/auth-files":
            return management_request(method, path, query=query, payload=payload)
        if path == "/api-call":
            return {"status_code": 200, "header": {}, "body": '{"ok":true}'}
        raise AssertionError((method, path))

    client.management_request.side_effect = ambiguous_management_request
    ambiguous = asyncio.run(
        adapter.observe_source(
            "openai",
            None,
            credential_ref,
            SOURCE_PROTOCOLS,
        )
    )

    assert ambiguous.outcome.value == "ambiguous"
    assert ambiguous.protocol is None
    assert ambiguous.authenticated is None


@pytest.mark.parametrize(
    ("protocol", "success_body", "request_error_body", "auth_error_body"),
    [
        (
            "anthropic",
            {"type": "message"},
            {"type": "error", "error": {"type": "invalid_request_error"}},
            {"type": "error", "error": {"type": "authentication_error"}},
        ),
        (
            "openai_responses",
            {"object": "response"},
            {"error": {"type": "invalid_request_error", "param": "input"}},
            {
                "error": {
                    "type": "invalid_request_error",
                    "code": "invalid_api_key",
                    "param": "input",
                }
            },
        ),
        (
            "openai_chat",
            {"object": "chat.completion"},
            {"error": {"type": "invalid_request_error", "param": "messages"}},
            {
                "error": {
                    "type": "invalid_request_error",
                    "code": "invalid_api_key",
                    "param": "messages",
                }
            },
        ),
    ],
)
def test_protocol_evidence_parser_requires_candidate_specific_response_shapes(
    protocol: str,
    success_body: dict,
    request_error_body: dict,
    auth_error_body: dict,
) -> None:
    assert _parse_protocol_authenticated_evidence(
        protocol,
        200,
        json.dumps(success_body),
    ) == _ProtocolEvidence(
        protocol=_ProtocolProof.PROVEN,
        authentication=_AuthenticationEvidence.ACCEPTED,
    )
    assert _parse_protocol_authenticated_evidence(
        protocol,
        400,
        json.dumps(request_error_body),
    ) == _ProtocolEvidence(
        protocol=_ProtocolProof.PROVEN,
        authentication=_AuthenticationEvidence.ACCEPTED,
    )
    assert _parse_protocol_authenticated_evidence(
        protocol,
        401,
        json.dumps(auth_error_body),
    ) == _ProtocolEvidence(
        protocol=_ProtocolProof.PROVEN,
        authentication=_AuthenticationEvidence.REJECTED,
    )
    assert _parse_protocol_authenticated_evidence(
        protocol,
        400,
        "{}",
    ) == _ProtocolEvidence(
        protocol=_ProtocolProof.UNPROVEN,
        authentication=_AuthenticationEvidence.UNKNOWN,
    )


@pytest.mark.parametrize(
    ("protocol", "status", "body"),
    [
        (
            "anthropic",
            500,
            {"type": "error", "error": {"type": "api_error"}},
        ),
        (
            "openai_responses",
            500,
            {"error": {"type": "server_error"}},
        ),
        (
            "openai_chat",
            500,
            {"error": {"type": "server_error"}},
        ),
        (
            "anthropic",
            429,
            {"type": "error", "error": {"type": "rate_limit_error"}},
        ),
        (
            "openai_responses",
            429,
            {"error": {"type": "rate_limit_exceeded"}},
        ),
        (
            "openai_chat",
            429,
            {"error": {"type": "rate_limit_exceeded"}},
        ),
        (
            "anthropic",
            400,
            {"type": "error", "error": {"type": "future_error"}},
        ),
        (
            "openai_responses",
            400,
            {"error": {"type": "future_error", "param": "input"}},
        ),
        (
            "openai_chat",
            400,
            {"error": {"type": "future_error", "param": "messages"}},
        ),
    ],
)
def test_protocol_evidence_table_defaults_shaped_non_auth_rows_to_unknown(
    protocol: str,
    status: int,
    body: dict,
) -> None:
    assert _parse_protocol_authenticated_evidence(
        protocol,
        status,
        json.dumps(body),
    ) == _ProtocolEvidence(
        protocol=_ProtocolProof.PROVEN,
        authentication=_AuthenticationEvidence.UNKNOWN,
    )


@pytest.mark.parametrize(
    ("protocol", "body"),
    [
        (
            "anthropic",
            {"type": "error", "error": {"type": "not_found_error"}},
        ),
        (
            "openai_responses",
            {"error": {"type": "invalid_request_error", "code": "model_not_found"}},
        ),
        (
            "openai_chat",
            {"error": {"type": "invalid_request_error", "code": "model_not_found"}},
        ),
    ],
)
def test_protocol_evidence_table_accepts_authenticated_model_errors(
    protocol: str,
    body: dict,
) -> None:
    assert _parse_protocol_authenticated_evidence(
        protocol,
        404,
        json.dumps(body),
    ) == _ProtocolEvidence(
        protocol=_ProtocolProof.PROVEN,
        authentication=_AuthenticationEvidence.ACCEPTED,
    )


def test_shared_openai_authentication_rejection_does_not_prove_a_protocol() -> None:
    body = json.dumps(
        {
            "error": {
                "type": "invalid_request_error",
                "code": "invalid_api_key",
                "param": None,
            }
        }
    )

    for protocol in ("openai_responses", "openai_chat"):
        assert _parse_protocol_authenticated_evidence(
            protocol,
            401,
            body,
        ) == _ProtocolEvidence(
            protocol=_ProtocolProof.UNPROVEN,
            authentication=_AuthenticationEvidence.REJECTED,
        )


def test_protocol_observation_consumers_cannot_classify_from_status_codes() -> None:
    module_path = Path(__file__).parents[1] / "vibe/model_hub_runtime/adapter.py"
    module_source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(module_source)
    assert _PROTOCOL_OBSERVATION_TAXONOMY.keys() == set(SOURCE_PROTOCOLS)
    assert _PROTOCOL_OBSERVATION_TAXONOMY["openai_responses"].request_path != _PROTOCOL_OBSERVATION_TAXONOMY[
        "openai_chat"
    ].request_path
    assert _PROTOCOL_OBSERVATION_TAXONOMY["openai_responses"].request_body == {
        "model": "__avibe_model_hub_probe__"
    }
    assert _PROTOCOL_OBSERVATION_TAXONOMY["openai_chat"].request_body == {
        "model": "__avibe_model_hub_probe__"
    }
    consumers = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"_probe_protocol_response", "_probe_oauth_protocol_response"}
    }

    assert consumers.keys() == {
        "_probe_protocol_response",
        "_probe_oauth_protocol_response",
    }
    assert "json={}" not in module_source
    assert '"data": "{}"' not in module_source
    assert "_endpoint_for_protocol(" not in module_source
    for consumer in consumers.values():
        consumer_source = ast.get_source_segment(module_source, consumer)
        assert consumer_source is not None
        assert "_PROTOCOL_OBSERVATION_TAXONOMY" in consumer_source
        calls = [node for node in ast.walk(consumer) if isinstance(node, ast.Call)]
        assert any(
            isinstance(call.func, ast.Name) and call.func.id == "_parse_protocol_authenticated_evidence"
            for call in calls
        )
        compared_statuses = {
            constant.value
            for compare in ast.walk(consumer)
            if isinstance(compare, ast.Compare)
            for constant in ast.walk(compare)
            if isinstance(constant, ast.Constant)
            and isinstance(constant.value, int)
            and not isinstance(constant.value, bool)
        }
        assert not compared_statuses

    auth_branch_offenders = []
    for function in (
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name != "_parse_protocol_authenticated_evidence"
    ):
        for branch in (node for node in ast.walk(function) if isinstance(node, (ast.If, ast.IfExp))):
            condition = ast.unparse(branch.test)
            branch_source = ast.unparse(branch)
            if any(token in condition for token in ("status", "error")) and "_AuthenticationEvidence" in branch_source:
                auth_branch_offenders.append((function.name, condition))
    assert not auth_branch_offenders


def test_protocol_observation_preserves_query_on_each_distinct_upstream_path() -> None:
    query = "api-version=2026-07-23"

    async def scenario() -> list[tuple[str, str, dict]]:
        requests: list[tuple[str, str, dict]] = []

        async def reject_empty_probe(request: web.Request) -> web.Response:
            requests.append((request.path, request.query_string, await request.json()))
            return web.json_response({"error": {"type": "invalid_request"}}, status=400)

        app = web.Application()
        app.router.add_post("/{tail:.*}", reject_empty_probe)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert site._server is not None
        port = site._server.sockets[0].getsockname()[1]
        try:
            for protocol in SOURCE_PROTOCOLS:
                await _probe_protocol_response(
                    vendor="custom",
                    protocol=protocol,
                    base_url=f"http://127.0.0.1:{port}/v1?{query}",
                    secret="test-observation-key",
                )
        finally:
            await runner.cleanup()
        return requests

    requests = asyncio.run(scenario())
    paths = [path for path, _query, _body in requests]

    assert len(paths) == len(SOURCE_PROTOCOLS)
    assert len(set(paths)) == len(paths)
    assert {request_query for _path, request_query, _body in requests} == {query}
    assert [body for _path, _query, body in requests] == [
        dict(_PROTOCOL_OBSERVATION_TAXONOMY[protocol].request_body)
        for protocol in SOURCE_PROTOCOLS
    ]


def test_openai_probe_requests_are_mutually_distinguishable() -> None:
    responses = _PROTOCOL_OBSERVATION_TAXONOMY["openai_responses"]
    chat = _PROTOCOL_OBSERVATION_TAXONOMY["openai_chat"]

    assert responses.request_path != chat.request_path
    assert "input" not in responses.request_body
    assert "messages" not in chat.request_body
    assert _parse_protocol_authenticated_evidence(
        "openai_responses",
        400,
        json.dumps({"error": {"type": "invalid_request_error", "param": "messages"}}),
    ) == _ProtocolEvidence(
        protocol=_ProtocolProof.UNPROVEN,
        authentication=_AuthenticationEvidence.UNKNOWN,
    )
    assert _parse_protocol_authenticated_evidence(
        "openai_chat",
        400,
        json.dumps({"error": {"type": "invalid_request_error", "param": "input"}}),
    ) == _ProtocolEvidence(
        protocol=_ProtocolProof.UNPROVEN,
        authentication=_AuthenticationEvidence.UNKNOWN,
    )


def test_model_discovery_preserves_query_when_appending_models_path() -> None:
    query = "api-version=2026-07-23"

    async def scenario() -> tuple[str, str, tuple[str, ...]]:
        request_target: tuple[str, str] | None = None

        async def list_models(request: web.Request) -> web.Response:
            nonlocal request_target
            request_target = (request.path, request.query_string)
            return web.json_response({"data": [{"id": "upstream-model"}]})

        app = web.Application()
        app.router.add_get("/{tail:.*}", list_models)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert site._server is not None
        port = site._server.sockets[0].getsockname()[1]
        try:
            models = await probe_models(
                vendor="custom",
                protocol="openai_chat",
                base_url=f"http://127.0.0.1:{port}/v1?{query}",
                secret="test-observation-key",
            )
        finally:
            await runner.cleanup()
        assert request_target is not None
        return request_target[0], request_target[1], models

    path, request_query, models = asyncio.run(scenario())

    assert path == "/v1/models"
    assert request_query == query
    assert models == ("upstream-model",)


def test_blocked_exact_hop_emits_one_supply_interruption(tmp_path: Path) -> None:
    source = _source("src_blocked01", "Blocked")
    service = _service(tmp_path, sources=[source])
    service.store.config.agents["claude"].routes["shared-model"] = ModelHubRouteConfig(
        hops=(ModelHubRouteHopConfig(source.id, "removed-model"),)
    )
    router = ModelHubRuntimeRouter(service=service)

    for _attempt in range(2):
        with pytest.raises(ModelHubError):
            asyncio.run(router.resolve("claude", "shared-model"))

    events = [event for event in service.list_events(limit=20) if event["kind"] == "supply_interrupted"]
    assert len(events) == 1
    assert events[0]["reason"] == "model_unsupported"


@pytest.mark.parametrize(
    ("route", "native_ready", "reason"),
    [
        (ModelHubRouteConfig(), True, "route_unconfigured"),
        (
            ModelHubRouteConfig(hops=(ModelHubRouteHopConfig("src_missing01", "shared-model"),)),
            True,
            "source_missing",
        ),
    ],
)
def test_supply_interruption_preserves_exact_structural_reason(
    tmp_path: Path,
    route: ModelHubRouteConfig,
    native_ready: bool,
    reason: str,
) -> None:
    source = _source("src_present01", "Present")
    service = _service(tmp_path, sources=[source])
    service.store.config.agents["claude"].routes["shared-model"] = route
    router = ModelHubRuntimeRouter(
        service=service,
        native_cli_ready=lambda _backend: native_ready,
    )

    with pytest.raises(ModelHubError):
        asyncio.run(router.resolve("claude", "shared-model"))

    event = next(item for item in service.list_events(limit=20) if item["kind"] == "supply_interrupted")
    assert event["reason"] == reason
    _assert_valid("resolution-event.schema.json", event)


def test_supply_interruption_preserves_native_process_reason(tmp_path: Path) -> None:
    source = _source(
        "src_native011",
        "Native",
        channel="native_cli",
        vendor="anthropic",
        protocol="anthropic",
    )
    service = _service(tmp_path, sources=[source])
    router = ModelHubRuntimeRouter(
        service=service,
        native_cli_ready=lambda _backend: False,
    )

    with pytest.raises(ModelHubError):
        asyncio.run(router.resolve("claude", "shared-model"))

    event = next(item for item in service.list_events(limit=20) if item["kind"] == "supply_interrupted")
    assert event["reason"] == "native_cli_unavailable"
    _assert_valid("resolution-event.schema.json", event)


def test_same_scope_concurrency_is_absent_and_sequential_control_is_present(
    tmp_path: Path,
) -> None:
    async def run() -> BoundedProvenanceStore:
        store = BoundedProvenanceStore(tmp_path / "concurrency.json")
        registry = TurnCorrelationRegistry(store)
        runtime = SimpleNamespace(
            settle_turn=lambda value, *, settled_by, ts, mode=None: registry.settle(
                value,
                settled_by=settled_by,
                ts=ts,
            )
        )
        controller = SimpleNamespace(
            model_hub_runtime=runtime,
            command_handler=SimpleNamespace(handle_stop=AsyncMock(return_value=True)),
            set_agent_status=lambda _session_id, _status: None,
        )
        manager = SessionTurnManager(controller)
        manager.flush_queue = AsyncMock(return_value=False)
        started = {turn_id: asyncio.Event() for turn_id in ("turn_one", "turn_two", "turn_sequential")}
        release = {turn_id: asyncio.Event() for turn_id in ("turn_one", "turn_two", "turn_sequential")}

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
    menu_model = _canonicalize_fixed_test_routes(service)["claude"]

    chain = service.agent_chain("claude", menu_model)
    assert [item["source_id"] for item in chain["chain"]] == [
        "src_cooling01",
        "src_primary01",
    ]
    assert chain["supply_state"] == "ok"
    assert chain["current"] == {
        "source_id": "src_primary01",
        "model_id": "shared-model",
    }
    assert all(item["channel"] == "hub" for item in chain["chain"])
    assert all(item["reason"] is None for item in chain["chain"])
    _assert_valid("agent-chain.schema.json", chain)

    probe = asyncio.run(service.probe_agent("claude", menu_model))
    assert probe["channel"] == "hub"
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
    network_model = _canonicalize_fixed_test_routes(network_service)["claude"]
    network = asyncio.run(network_service.probe_agent("claude", network_model))
    assert network["reachable"] is False
    assert network["latency_ms"] is None
    assert network["error"] == "models.source.cooldown.network"
    _assert_valid("probe-result.schema.json", network)

    timeout_service = _service(
        tmp_path / "timeout",
        sources=[_source("src_primary01", "Primary")],
        outcomes=[_outcome(RawOutcomeKind.TIMEOUT)],
    )
    timeout_model = _canonicalize_fixed_test_routes(timeout_service)["claude"]
    timeout = asyncio.run(timeout_service.probe_agent("claude", timeout_model))
    assert timeout["reachable"] is False
    assert timeout["latency_ms"] is None
    assert timeout["error"] == "models.source.cooldown.timeout"
    assert timeout_service.store.load().sources[0].state.detail_key == ("models.source.cooldown.timeout")
    assert timeout_service.events.list(limit=10)[0]["reason"] == "network"
    _assert_valid("probe-result.schema.json", timeout)

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
    rate_model = _canonicalize_fixed_test_routes(rate_service)["claude"]
    rate_limited = asyncio.run(rate_service.probe_agent("claude", rate_model))
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
    unclassified_model = _canonicalize_fixed_test_routes(unclassified_service)["claude"]
    unclassified = asyncio.run(unclassified_service.probe_agent("claude", unclassified_model))
    assert unclassified["reachable"] is False
    assert isinstance(unclassified["latency_ms"], int)
    assert unclassified["error"] == "models.source.error.unclassified"
    assert unclassified_service.store.load().sources[0].state.status == "error"
    _assert_valid("probe-result.schema.json", unclassified)

    request_error_service = _service(
        tmp_path / "request-error",
        sources=[_source("src_primary01", "Primary")],
        outcomes=[
            _outcome(
                RawOutcomeKind.HTTP_ERROR,
                status=400,
                code="invalid_parameter",
            )
        ],
    )
    request_error_model = _canonicalize_fixed_test_routes(request_error_service)["claude"]
    request_error = asyncio.run(request_error_service.probe_agent("claude", request_error_model))
    assert request_error["reachable"] is False
    assert isinstance(request_error["latency_ms"], int)
    assert request_error["error"] == "models.source.error.unclassified"
    assert request_error_service.store.load().sources[0].state.status == "standby"
    assert request_error_service.events.list(limit=10) == []
    _assert_valid("probe-result.schema.json", request_error)

    anthropic_not_found = _service(
        tmp_path / "anthropic-not-found",
        sources=[_source("src_primary01", "Primary")],
        outcomes=[
            _outcome(
                RawOutcomeKind.HTTP_ERROR,
                status=404,
                code="not_found_error",
            )
        ],
    )
    not_found_model = _canonicalize_fixed_test_routes(anthropic_not_found)["claude"]
    not_found = asyncio.run(anthropic_not_found.probe_agent("claude", not_found_model))
    assert not_found["reachable"] is False
    assert anthropic_not_found.store.load().sources[0].state.status == "standby"
    assert anthropic_not_found.events.list(limit=10) == []
    _assert_valid("probe-result.schema.json", not_found)


def test_probe_401_uses_exact_credential_refresh_capability(tmp_path: Path) -> None:
    static_source = _source("src_staticprobe", "Static")
    static_service = _service(
        tmp_path / "static",
        sources=[static_source],
        outcomes=[
            _outcome(
                RawOutcomeKind.HTTP_ERROR,
                status=401,
                code="unauthorized",
                source_id=static_source.id,
            )
        ],
    )
    static_model = _canonicalize_fixed_test_routes(static_service)["claude"]
    static_adapter = cast(ProbeAdapter, static_service.adapter)

    static_probe = asyncio.run(static_service.probe_agent("claude", static_model))

    assert static_probe["reachable"] is False
    assert static_probe["error"] == ("models.source.needs_action.credential_revoked")
    assert static_adapter.invocations == [(static_source.id, "shared-model", "claude")]
    assert static_adapter.capability_queries == [static_source.credential_ref]
    assert static_service.store.load().sources[0].state.detail_key == ("models.source.needs_action.credential_revoked")

    refreshable_source = _source("src_refreshprobe", "Refreshable")
    refreshable_service = _service(
        tmp_path / "refreshable",
        sources=[refreshable_source],
        outcomes=[
            _outcome(
                RawOutcomeKind.HTTP_ERROR,
                status=401,
                code="unauthorized",
                source_id=refreshable_source.id,
            ),
            _outcome(
                RawOutcomeKind.SUCCESS,
                status=200,
                source_id=refreshable_source.id,
            ),
        ],
    )
    refreshable_model = _canonicalize_fixed_test_routes(refreshable_service)["claude"]
    refreshable_adapter = cast(ProbeAdapter, refreshable_service.adapter)
    assert refreshable_source.credential_ref is not None
    refreshable_adapter.refreshable_credential_refs.add(refreshable_source.credential_ref)

    refreshable_probe = asyncio.run(refreshable_service.probe_agent("claude", refreshable_model))

    assert refreshable_probe["reachable"] is True
    assert refreshable_adapter.invocations == [
        (refreshable_source.id, "shared-model", "claude"),
        (refreshable_source.id, "shared-model", "claude"),
    ]
    assert refreshable_adapter.capability_queries == [refreshable_source.credential_ref]


@pytest.mark.parametrize(
    ("backend", "source_protocol", "request_protocol", "request_keys"),
    [
        (
            "claude",
            source_protocol,
            "anthropic",
            {"model", "max_tokens", "messages"},
        )
        for source_protocol in SOURCE_PROTOCOLS
    ]
    + [
        (
            "codex",
            source_protocol,
            "openai_responses",
            {"model", "max_output_tokens", "input"},
        )
        for source_protocol in SOURCE_PROTOCOLS
    ]
    + [
        (
            "opencode",
            source_protocol,
            source_protocol,
            (
                {"model", "max_output_tokens", "input"}
                if source_protocol == "openai_responses"
                else {"model", "max_tokens", "messages"}
            ),
        )
        for source_protocol in SOURCE_PROTOCOLS
    ],
)
def test_probe_request_matches_live_backend_protocol_matrix(
    tmp_path: Path,
    backend: str,
    source_protocol: str,
    request_protocol: str,
    request_keys: set[str],
) -> None:
    source = _source("src_primary01", "Primary")
    source.protocol = source_protocol
    service = _service(
        tmp_path,
        sources=[source],
        outcomes=[_outcome(RawOutcomeKind.SUCCESS)],
    )

    requested_model = "openai/shared-model" if backend == "opencode" else "shared-model"
    result = asyncio.run(service.probe_agent(backend, requested_model))

    assert result["reachable"] is True
    assert service.adapter.invocations == [("src_primary01", "shared-model", backend)]
    request = service.adapter.requests[0]
    assert request.protocol == request_protocol
    assert set(request) == request_keys


def test_native_chain_visibility_and_probe_readiness(tmp_path: Path) -> None:
    native = _source(
        "src_nativecli1",
        "Native CLI",
        channel="native_cli",
    )
    service = _service(tmp_path, sources=[native])

    chain = service.agent_chain("codex", "shared-model")
    assert chain["chain"] == [
        {
            "source_id": native.id,
            "model_id": "shared-model",
            "channel": "native_cli",
            "health": "healthy",
            "runnable": True,
            "reason": None,
            "retry_at": None,
        }
    ]
    assert chain["current"] == {"source_id": native.id, "model_id": "shared-model"}
    assert chain["supply_state"] == "ok"
    _assert_valid("agent-chain.schema.json", chain)

    probe = asyncio.run(service.probe_agent("codex", "shared-model"))
    assert probe == {
        "contract_version": 5,
        "backend": "codex",
        "channel": "native_cli",
        "reachable": True,
        "source_id": native.id,
        "model_id": "shared-model",
        "latency_ms": None,
        "error": None,
    }
    assert service.adapter.invocations == []
    _assert_valid("probe-result.schema.json", probe)


def test_native_unavailability_is_orthogonal_to_health(
    tmp_path: Path,
) -> None:
    native = _source(
        "src_nativecli1",
        "Native CLI",
        channel="native_cli",
        status="cooldown",
        retry_at=(NOW + timedelta(minutes=5)).isoformat(),
    )
    service = _service(tmp_path, sources=[native])
    service.native_source_ready = lambda _backend, _source: False

    chain = service.agent_chain("codex", "shared-model")
    assert chain["chain"][0]["health"] == "cooldown"
    assert chain["chain"][0]["reason"] == "native_cli_unavailable"
    assert chain["chain"][0]["runnable"] is False
    assert chain["current"] is None
    assert chain["supply_state"] == "interrupted"
    _assert_valid("agent-chain.schema.json", chain)


def test_native_probe_rechecks_readiness_after_selection(
    tmp_path: Path,
) -> None:
    native = _source(
        "src_nativecli1",
        "Native CLI",
        channel="native_cli",
    )
    service = _service(tmp_path, sources=[native])
    readiness = iter((True, False))
    service.native_source_ready = lambda _backend, _source: next(readiness)

    probe = asyncio.run(service.probe_agent("codex", "shared-model"))
    assert probe["channel"] == "native_cli"
    assert probe["reachable"] is False
    assert probe["latency_ms"] is None
    assert probe["error"] == "models.probe.native_cli_unavailable"
    assert service.adapter.invocations == []
    _assert_valid("probe-result.schema.json", probe)


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
    assert exc_info.value.data == {
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
    menu_model = _canonicalize_fixed_test_routes(service)["claude"]

    probe = asyncio.run(service.probe_agent("claude", menu_model))
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

    agents = {item["backend"]: item for item in service.list_agents()}
    assert agents["claude"]["supply_status"] == "interrupted"
    assert agents["codex"]["supply_status"] == "interrupted"

    service.store.config.sources[0].state = ModelHubSourceStateConfig(status="standby")
    recovered_agents = {item["backend"]: item for item in service.list_agents()}
    assert recovered_agents["claude"]["supply_status"] == "ok"
    assert recovered_agents["codex"]["supply_status"] == "ok"
    assert service.list_events(limit=20)[0] == event

    service.store.config.sources.clear()
    assert service.list_events(limit=20)[0] == event


def test_retired_mapping_event_is_rejected_and_channel_switch_retains_subject() -> None:
    with pytest.raises(ValueError):
        build_resolution_event(
            agent="codex",
            kind=cast(EventKind, "mapping" + "_applied"),
            model_id="target-model",
            reason=cast(EventReason, "map" + "ping"),
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

    for text in (channel_switch.human_en, channel_switch.human_zh):
        assert "Primary source" in text


def test_resolution_event_reason_contract_matches_runtime_vocabulary() -> None:
    schema = json.loads((CONTRACTS / "resolution-event.schema.json").read_text(encoding="utf-8"))
    authority = tuple(EVENT_REASON_AUTHORITY)
    assert tuple(schema["properties"]["reason"]["enum"]) == authority

    locale_reasons = {
        locale: json.loads((Path(__file__).parents[1] / f"vibe/i18n/{locale}.json").read_text(encoding="utf-8"))[
            "modelHub"
        ]["events"]["reason"]
        for locale in ("en", "zh")
    }
    assert all(tuple(reasons) == authority for reasons in locale_reasons.values())
    assert all(
        event_reason_label(reason, locale) != f"modelHub.events.reason.{reason}"
        for locale in locale_reasons
        for reason in authority
    )


@pytest.mark.parametrize("reason", tuple(EVENT_REASON_AUTHORITY))
def test_every_authoritative_reason_has_an_event_emission_path(reason: EventReason) -> None:
    reason_class = EVENT_REASON_AUTHORITY[reason]
    fields: dict = {
        "agent": "system",
        "model_id": None,
        "reason": reason,
        "now": NOW,
    }
    if reason_class == "structural":
        fields.update(agent="codex", kind="supply_interrupted", model_id="model")
    elif reason_class == "self_healing":
        fields.update(kind="cooldown", from_source="src_reason01")
    elif reason_class == "non_self_healing":
        fields.update(kind="needs_action", from_source="src_reason01")
    elif reason_class == "request_scoped":
        fields.update(
            agent="codex",
            kind="switch",
            model_id="model",
            from_source="src_reason01",
            to_source="src_reason02",
        )
    elif reason == "recovery":
        fields.update(kind="recover", to_source="src_reason01")
    else:
        fields.update(
            kind="channel_switch",
            from_source="src_reason01",
            to_source="src_reason01",
        )

    event = build_resolution_event(**fields)

    assert event.reason == reason
    assert event.human_en
    assert event.human_zh


def test_shared_source_cooldown_emits_only_on_state_transition(
    tmp_path: Path,
) -> None:
    source = _source("src_primary01", "Shared source")
    service = _service(tmp_path, sources=[source])
    menu_models = _canonicalize_fixed_test_routes(service)
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
            model_id=menu_models["claude"],
        )
        await service._cooldown(
            source,
            decision,
            agent="codex",
            model_id=menu_models["codex"],
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
        "_known_turn",
        lambda turn_id: (
            ("codex", "direct")
            if turn_id == "turn_direct"
            else (("codex", "hub") if turn_id == "turn_ambiguous" else (None, None))
        ),
    )

    service.store.config.agents["codex"].mode = "hub"
    with pytest.raises(ModelHubError) as direct:
        service.get_turn_provenance("turn_direct")
    assert direct.value.code == "provenance_unavailable"
    assert direct.value.detail == "models.provenance.direct_mode"

    service.store.config.agents["codex"].mode = "direct"
    with pytest.raises(ModelHubError) as ambiguous:
        service.get_turn_provenance("turn_ambiguous")
    assert ambiguous.value.code == "provenance_unavailable"
    assert ambiguous.value.detail == "models.provenance.attribution_ambiguous"

    with pytest.raises(ModelHubError) as unknown:
        service.get_turn_provenance("turn_unknown")
    assert unknown.value.code == "turn_not_found"
    assert unknown.value.status == 404


def test_turn_mode_marker_is_deleted_with_its_turn_record(
    monkeypatch,
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'state.db'}")
    metadata.create_all(engine)
    now = NOW.isoformat()
    with engine.begin() as conn:
        conn.execute(
            agent_sessions.insert().values(
                id="ses_turn_mode",
                agent_backend="codex",
                agent_variant="codex",
                session_anchor="anchor",
                native_session_id="",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            messages.insert().values(
                id="msg_turn_mode",
                session_id="ses_turn_mode",
                platform="avibe",
                author="user",
                type="user",
                source="user",
                content_json="{}",
                metadata_json=json.dumps({"turn_id": "turn_direct"}),
                created_at=now,
                updated_at=now,
            )
        )

    service = _service(
        tmp_path,
        sources=[_source("src_primary01", "Primary")],
    )
    monkeypatch.setattr(
        "core.handlers.model_hub.service.get_cached_sqlite_engine",
        lambda: engine,
    )
    service.note_turn_mode("turn_direct", "direct")
    with engine.connect() as conn:
        stored_mode = conn.execute(
            select(
                messages.c.metadata_json,
            ).where(messages.c.id == "msg_turn_mode")
        ).scalar_one()
    assert json.loads(stored_mode)["model_hub_mode"] == "direct"

    with pytest.raises(ModelHubError) as unavailable:
        service.get_turn_provenance("turn_direct")
    assert unavailable.value.code == "provenance_unavailable"
    assert unavailable.value.detail == "models.provenance.direct_mode"

    with engine.begin() as conn:
        conn.execute(delete(messages).where(messages.c.id == "msg_turn_mode"))

    with pytest.raises(ModelHubError) as deleted:
        service.get_turn_provenance("turn_direct")
    assert deleted.value.code == "turn_not_found"


def test_known_opencode_turn_is_fail_closed_but_not_unknown(
    monkeypatch,
    tmp_path: Path,
) -> None:
    store = BoundedProvenanceStore(tmp_path / "opencode-provenance.json")
    registry = TurnCorrelationRegistry(store)
    token = registry.credentials(
        "opencode",
        "opencode:shared-server",
        "turn_opencode",
    )

    assert (
        registry.begin_gateway_request(
            backend="opencode",
            token=token,
            requested_model_id="openai/shared-model",
        )
        is None
    )
    registry.settle(
        "turn_opencode",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
    )
    assert store.get("turn_opencode") is None

    service = _service(
        tmp_path,
        sources=[_source("src_primary01", "Primary")],
    )
    service.provenance = store
    monkeypatch.setattr(
        service,
        "_known_turn",
        lambda turn_id: (("opencode", None) if turn_id == "turn_opencode" else (None, None)),
    )

    with pytest.raises(ModelHubError) as unavailable:
        service.get_turn_provenance("turn_opencode")
    assert unavailable.value.code == "provenance_unavailable"
    assert unavailable.value.detail == "models.provenance.attribution_ambiguous"
