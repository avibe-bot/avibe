"""Second-wave Model Hub scenarios for the frozen turn contracts."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import pytest
from jsonschema import Draft7Validator

from core.handlers.model_hub.adapter import RawCallOutcome, RawOutcomeKind
from core.handlers.model_hub.classification import ResolutionDecision
from core.handlers.model_hub.provenance import (
    BoundedProvenanceStore,
    TURN_OUTCOME_RENDERING_AUTHORITY,
    TurnCorrelationRegistry,
    produce_turn_outcome,
    project_turn_outcome_copy,
)
from core.handlers.model_hub.service import ModelHubError
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from core.run_settlement import (
    SETTLED_BY_NO_TERMINAL_RESULT,
    SETTLED_BY_STOPPED,
    SETTLED_BY_TERMINAL_RESULT,
)
from modules.agents.model_hub import ModelHubRuntimeRouter, resolve_model_hub_turn
from tests.scenario_harness.model_hub import (
    MemoryModelHubStore,
    ModelHubScenarioAdapter,
    ScenarioCallResult,
    config_with_sources,
    fixed_model,
    service_for,
    source,
)


CONTRACTS = Path("docs/plans/model-hub-contracts")


@pytest.fixture(autouse=True)
def _enable_model_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIBE_MODEL_HUB_ENABLED", "1")


class EngineLossAdapter(ModelHubScenarioAdapter):
    async def invoke(self, source_id, model_id, request, stream, origin):
        self.invocations.append((source_id, model_id, origin))
        self.requests.append(request)
        raise RuntimeError("scenario engine unavailable")


class MidTurnEngineLossHandle:
    def __init__(self, source_id: str, model_id: str) -> None:
        self._outcome = RawCallOutcome(
            kind=RawOutcomeKind.NETWORK_ERROR,
            http_status=None,
            error_code="engine_down",
            redacted_message=None,
            stream_started=True,
            model_id=model_id,
            source_id=source_id,
        )

    @property
    def stream(self):
        async def chunks():
            yield b"data: partial\n\n"
            raise RuntimeError("scenario engine disappeared")

        return chunks()

    @property
    def observed(self):
        """This double reports nothing about the body until its consumer reads it."""

        return None

    @property
    def outcome_available(self) -> bool:
        return True

    async def outcome(self):
        return self._outcome

    async def close_stream(self) -> None:
        return None


class MidTurnEngineLossAdapter(ModelHubScenarioAdapter):
    async def invoke(self, source_id, model_id, request, stream, origin):
        self.invocations.append((source_id, model_id, origin))
        self.requests.append(request)
        return MidTurnEngineLossHandle(source_id, model_id)


def _provenance_schema() -> dict:
    return json.loads((CONTRACTS / "turn-provenance.schema.json").read_text())


def _turn_copy_rows() -> list[dict[str, str]]:
    lines = Path("docs/plans/model-hub.md").read_text().splitlines()
    heading = next(index for index, line in enumerate(lines) if line.startswith("**Turn-outcome copy matrix"))
    header = next(index for index, line in enumerate(lines[heading:], start=heading) if line.startswith("| Decision |"))
    rows: list[dict[str, str]] = []
    for line in lines[header + 2 :]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        rows.append(
            {
                "decision": cells[0].strip("`"),
                "outcome": cells[1].strip("`"),
                "rendering": cells[4],
            }
        )
    return rows


def _locale_value(locale: dict, key: str) -> object:
    value: object = locale
    for part in key.split("."):
        assert isinstance(value, dict)
        value = value[part]
    return value


def _projection_for_copy_variant(
    decision: str,
    variant: str,
) -> object:
    kwargs: dict[str, object] = {}
    if variant in {"waiting", "interrupted"}:
        menu_model = fixed_model("claude")
        supplied = source(
            "src_copyvariant",
            [menu_model],
            status=("cooldown" if variant == "waiting" else "needs_action"),
            retry_at=(
                "2026-08-13T01:00:00+00:00" if variant == "waiting" else None
            ),
        )
        if variant == "interrupted":
            supplied.state.detail_key = (
                "models.source.needs_action.credential_revoked"
            )
        config = config_with_sources(
            [supplied],
            backend="claude",
            menu_model=menu_model,
        )
        resolution = resolve_model_hub_turn(
            config,
            "claude",
            menu_model,
            now=datetime(2026, 8, 13, tzinfo=timezone.utc),
        )
        kwargs.update(config=config, resolution=resolution)
        if decision == "turn.streamed_fallback":
            kwargs.update(
                attempted_hop=("src_attempted", menu_model),
                source_transition_persisted=True,
            )
    elif variant == "waiting_without_retry":
        menu_model = fixed_model("claude")
        supplied = source("src_copyready", [menu_model])
        config = config_with_sources(
            [supplied],
            backend="claude",
            menu_model=menu_model,
        )
        resolution = resolve_model_hub_turn(
            config,
            "claude",
            menu_model,
            now=datetime(2026, 8, 13, tzinfo=timezone.utc),
        )
        kwargs.update(config=config, resolution=resolution)
    elif variant == "transition_unpersisted":
        kwargs["source_transition_persisted"] = False
    elif variant == "next_current":
        menu_model = fixed_model("claude")
        supplied = source("src_copynext", [menu_model])
        config = config_with_sources(
            [supplied],
            backend="claude",
            menu_model=menu_model,
        )
        resolution = resolve_model_hub_turn(
            config,
            "claude",
            menu_model,
            now=datetime(2026, 8, 13, tzinfo=timezone.utc),
        )
        kwargs.update(
            config=config,
            resolution=resolution,
            attempted_hop=("src_attempted", menu_model),
            source_transition_persisted=True,
        )
    return produce_turn_outcome(
        decision,
        stream_started=variant == "stream_started",
        **kwargs,
    )


def _raw_outcome(
    kind: RawOutcomeKind,
    *,
    status: int | None = None,
    error_code: str | None = None,
) -> RawCallOutcome:
    return RawCallOutcome(
        kind=kind,
        http_status=status,
        error_code=error_code,
        redacted_message=None,
        stream_started=False,
        model_id="scenario-model",
        source_id="src_outcome01",
    )


async def _post(launch) -> tuple[int, dict]:
    async with aiohttp.ClientSession(trust_env=False) as client:
        async with client.post(
            f"{launch.gateway_base_url}/v1/messages",
            headers={"Authorization": f"Bearer {launch.gateway_token}"},
            json={"model": launch.runtime_model, "messages": [], "stream": False},
        ) as response:
            return response.status, await response.json()


async def _post_stream(launch) -> tuple[int, bytes, BaseException | None]:
    async with aiohttp.ClientSession(trust_env=False) as client:
        async with client.post(
            f"{launch.gateway_base_url}/v1/messages",
            headers={"Authorization": f"Bearer {launch.gateway_token}"},
            json={"model": launch.runtime_model, "messages": [], "stream": True},
        ) as response:
            body = bytearray()
            try:
                async for chunk in response.content.iter_any():
                    body.extend(chunk)
            except aiohttp.ClientPayloadError as error:
                return response.status, bytes(body), error
            return response.status, bytes(body), None


def test_mh_turn_takeover_is_silent_and_provenance_names_the_serving_hop(tmp_path: Path) -> None:
    """MH-TAKEOVER-001: fallback serves in order, or settles exhausted after the same route ends."""

    menu_model = fixed_model("claude")
    first = source("src_takeover01", [menu_model])
    second = source("src_takeover02", [menu_model])
    store = MemoryModelHubStore(
        config_with_sources(
            [first, second],
            backend="claude",
            menu_model=menu_model,
            hops=[(first.id, menu_model), (second.id, menu_model)],
        )
    )
    adapter = ModelHubScenarioAdapter(
        invoke_results=(
            ScenarioCallResult(
                RawOutcomeKind.HTTP_ERROR,
                status=429,
                error_code="quota_exhausted",
            ),
            ScenarioCallResult(RawOutcomeKind.SUCCESS, body=b'{"answer":"ok"}'),
        )
    )
    service = service_for(
        tmp_path,
        store,
        adapter,
        now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)

    exhausted_first = source("src_exhausted01", [menu_model])
    exhausted_last = source("src_exhausted02", [menu_model])
    exhausted_store = MemoryModelHubStore(
        config_with_sources(
            [exhausted_first, exhausted_last],
            backend="claude",
            menu_model=menu_model,
            hops=[
                (exhausted_first.id, menu_model),
                (exhausted_last.id, menu_model),
            ],
        )
    )
    exhausted_adapter = ModelHubScenarioAdapter(
        invoke_results=(
            ScenarioCallResult(
                RawOutcomeKind.HTTP_ERROR,
                status=429,
                error_code="quota_exhausted",
            ),
            ScenarioCallResult(
                RawOutcomeKind.HTTP_ERROR,
                status=429,
                error_code="quota_exhausted",
            ),
        )
    )
    exhausted_service = service_for(
        tmp_path / "exhausted",
        exhausted_store,
        exhausted_adapter,
        now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    exhausted_gateway = ModelHubTurnGateway(exhausted_service)
    exhausted_router = ModelHubRuntimeRouter(
        service=exhausted_service,
        turn_gateway=exhausted_gateway,
    )

    async def exercise() -> tuple[dict, dict]:
        try:
            launch = await router.resolve(
                "claude",
                menu_model,
                process_scope="scenario-takeover",
                turn_id="turn_takeover01",
            )
            status, body = await _post(launch)
            router.settle_turn(
                "turn_takeover01",
                settled_by="terminal_result",
                ts="2026-01-01T00:00:01+00:00",
            )
            served_result = {
                "status": status,
                "body": body,
                "provenance": service.get_turn_provenance("turn_takeover01"),
            }
            exhausted_launch = await exhausted_router.resolve(
                "claude",
                menu_model,
                process_scope="scenario-exhausted",
                turn_id="turn_exhausted01",
            )
            exhausted_status, exhausted_body = await _post(exhausted_launch)
            exhausted_router.settle_turn(
                "turn_exhausted01",
                settled_by="no_terminal_result",
                ts="2026-01-01T00:00:01+00:00",
            )
            exhausted_result = {
                "status": exhausted_status,
                "body": exhausted_body,
                "provenance": exhausted_service.get_turn_provenance("turn_exhausted01"),
            }
            return served_result, exhausted_result
        finally:
            await gateway.close()
            await exhausted_gateway.close()

    result, exhausted_result = asyncio.run(exercise())
    assert result["status"] == 200
    assert result["body"] == {"answer": "ok"}
    assert [source_id for source_id, _model_id, _origin in adapter.invocations] == [
        first.id,
        second.id,
    ]
    assert result["provenance"]["outcome"] == "served"
    assert result["provenance"]["failed_attempts"][0]["source_id"] == first.id
    assert result["provenance"]["served"]["source_id"] == second.id
    Draft7Validator(_provenance_schema()).validate(result["provenance"])
    assert exhausted_result["status"] == 503
    assert (
        exhausted_result["body"]["error"]["code"]
        == "mapping_target_unavailable"
    )
    assert exhausted_result["provenance"]["outcome"] == "exhausted"
    assert [attempt["source_id"] for attempt in exhausted_result["provenance"]["failed_attempts"]] == [
        exhausted_first.id,
        exhausted_last.id,
    ]
    assert exhausted_result["provenance"]["served"] is None
    Draft7Validator(_provenance_schema()).validate(exhausted_result["provenance"])


def test_mh_engine_loss_is_terminal_without_source_mutation_or_replay(tmp_path: Path) -> None:
    """MH-ENGINE-001: a local engine loss is terminal and never becomes a Source failure."""

    menu_model = fixed_model("claude")
    upstream = source("src_engine01", [menu_model])
    store = MemoryModelHubStore(config_with_sources([upstream], backend="claude", menu_model=menu_model))
    adapter = EngineLossAdapter()
    service = service_for(tmp_path, store, adapter)
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)

    async def exercise() -> tuple[int, dict, str, dict]:
        try:
            launch = await router.resolve(
                "claude",
                menu_model,
                process_scope="scenario-engine",
                turn_id="turn_engine01",
            )
            status, body = await _post(launch)
            router.settle_turn(
                "turn_engine01",
                settled_by="no_terminal_result",
                ts="2026-01-01T00:00:01+00:00",
            )
            return (
                status,
                body,
                store.load().sources[0].state.status,
                service.get_turn_provenance("turn_engine01"),
            )
        finally:
            await gateway.close()

    status, body, source_status, provenance = asyncio.run(exercise())
    assert status == 503
    assert body["error"]["code"] == "engine_down"
    assert source_status == "standby"
    assert len(adapter.invocations) == 1
    assert provenance["outcome"] == "failed_terminal"
    assert provenance["terminal_error"]["reason"] == "engine_down"
    assert provenance["terminal_error"]["source_id"] is None
    Draft7Validator(_provenance_schema()).validate(provenance)


def test_mh_mid_turn_engine_loss_never_replays_after_output_starts(tmp_path: Path) -> None:
    """MH-ENGINE-MIDTURN-001: once output is visible, local engine loss terminates without a next-hop walk."""

    menu_model = fixed_model("claude")
    first = source("src_midengine1", [menu_model])
    backup = source("src_midengine2", [menu_model])
    store = MemoryModelHubStore(
        config_with_sources(
            [first, backup],
            backend="claude",
            menu_model=menu_model,
            hops=[(first.id, menu_model), (backup.id, menu_model)],
        )
    )
    adapter = MidTurnEngineLossAdapter()
    service = service_for(tmp_path, store, adapter)
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)

    async def exercise() -> tuple[int, bytes, dict]:
        try:
            launch = await router.resolve(
                "claude",
                menu_model,
                process_scope="scenario-mid-engine",
                turn_id="turn_midengine01",
            )
            status, body, _transport_error = await _post_stream(launch)
            router.settle_turn(
                "turn_midengine01",
                settled_by="no_terminal_result",
                ts="2026-01-01T00:00:01+00:00",
            )
            return status, body, service.get_turn_provenance("turn_midengine01")
        finally:
            await gateway.close()

    status, body, provenance = asyncio.run(exercise())
    assert status == 200
    assert body == b"data: partial\n\n"
    assert [source_id for source_id, _model_id, _origin in adapter.invocations] == [first.id]
    assert [item.state.status for item in store.load().sources] == ["standby", "standby"]
    assert provenance["outcome"] == "failed_terminal"
    assert provenance["terminal_error"]["reason"] == "engine_down"
    assert provenance["terminal_error"]["stream_started"] is True
    assert provenance["terminal_error"]["source_id"] is None
    Draft7Validator(_provenance_schema()).validate(provenance)


def test_mh_static_credential_failure_does_not_retry_or_claim_takeover(tmp_path: Path) -> None:
    """MH-CREDENTIAL-001: a 401 retries once only when the stored credential can refresh."""

    menu_model = fixed_model("claude")
    static_source = source("src_credential01", [menu_model])
    static_store = MemoryModelHubStore(config_with_sources([static_source], backend="claude", menu_model=menu_model))
    static_adapter = ModelHubScenarioAdapter(
        invoke_results=(
            ScenarioCallResult(RawOutcomeKind.HTTP_ERROR, status=401),
            ScenarioCallResult(RawOutcomeKind.SUCCESS, body=b"should-not-run"),
        )
    )
    static_service = service_for(tmp_path / "static", static_store, static_adapter)
    static_gateway = ModelHubTurnGateway(static_service)
    static_router = ModelHubRuntimeRouter(
        service=static_service,
        turn_gateway=static_gateway,
    )

    refresh_ref = "cred_refreshable01"
    refresh_source = source(
        "src_credential02",
        [menu_model],
        credential_ref=refresh_ref,
    )
    refresh_store = MemoryModelHubStore(config_with_sources([refresh_source], backend="claude", menu_model=menu_model))
    refresh_adapter = ModelHubScenarioAdapter(
        invoke_results=(
            ScenarioCallResult(RawOutcomeKind.HTTP_ERROR, status=401),
            ScenarioCallResult(RawOutcomeKind.SUCCESS, body=b'{"answer":"ok"}'),
        ),
        refreshable_credential_refs=(refresh_ref,),
    )
    refresh_service = service_for(tmp_path / "refresh", refresh_store, refresh_adapter)
    refresh_gateway = ModelHubTurnGateway(refresh_service)
    refresh_router = ModelHubRuntimeRouter(
        service=refresh_service,
        turn_gateway=refresh_gateway,
    )

    async def exercise() -> tuple[int, dict]:
        try:
            static_launch = await static_router.resolve(
                "claude",
                menu_model,
                turn_id="turn_credential01",
            )
            await _post(static_launch)
            refresh_launch = await refresh_router.resolve(
                "claude",
                menu_model,
                turn_id="turn_credential02",
            )
            return await _post(refresh_launch)
        finally:
            await static_gateway.close()
            await refresh_gateway.close()

    refresh_status, refresh_body = asyncio.run(exercise())
    assert len(static_adapter.invocations) == 1
    assert static_store.load().sources[0].state.status == "needs_action"
    assert static_store.load().sources[0].state.detail_key == "models.source.needs_action.credential_revoked"
    assert refresh_status == 200
    assert refresh_body == {"answer": "ok"}
    assert len(refresh_adapter.invocations) == 2
    assert refresh_store.load().sources[0].state.status == "standby"


def test_mh_turn_outcomes_and_cancel_are_closed_provenance_products(tmp_path: Path) -> None:
    """MH-TURN-RESULT-001: runtime settlement produces every outcome and no value outside the schema."""

    schema = _provenance_schema()
    store = BoundedProvenanceStore(tmp_path / "provenance.json")
    registry = TurnCorrelationRegistry(store)
    records: list[dict] = []

    def terminalizer(turn_id: str):
        token = registry.prepare_gateway_turn(
            backend="claude",
            token=registry.credentials("claude", f"scope-{turn_id}", turn_id),
            requested_model_id="scenario-model",
            resolved_model_id="scenario-model",
            source_id="src_outcome01",
            via_mapping=False,
        )
        return registry.gateway_terminalizer(backend="claude", token=token)

    served = terminalizer("turn_served01")
    with served:
        served.finish_attempt(
            outcome=_raw_outcome(RawOutcomeKind.SUCCESS),
            decision=ResolutionDecision("return"),
        )
    registry.settle(
        "turn_served01",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
    )

    exhausted = terminalizer("turn_exhausted01")
    with exhausted:
        exhausted.finish_attempt(
            outcome=_raw_outcome(RawOutcomeKind.HTTP_ERROR, status=429),
            decision=ResolutionDecision("fallback", reason="rate_limited"),
        )
    registry.settle(
        "turn_exhausted01",
        settled_by=SETTLED_BY_NO_TERMINAL_RESULT,
    )

    failed = terminalizer("turn_failed01")
    with failed:
        failed.finish_attempt(
            outcome=_raw_outcome(
                RawOutcomeKind.HTTP_ERROR,
                status=400,
                error_code="request_too_large",
            ),
            decision=ResolutionDecision(
                "surface",
                error_code="upstream_request_invalid",
            ),
        )
    registry.settle(
        "turn_failed01",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
    )

    terminalizer("turn_drop01")
    registry.settle(
        "turn_drop01",
        settled_by=SETTLED_BY_NO_TERMINAL_RESULT,
    )

    registry.mark_no_candidate(
        backend="claude",
        process_scope="scope-turn_no_candidate01",
        turn_id="turn_no_candidate01",
        requested_model_id="scenario-model",
        supply_state="interrupted",
    )
    registry.settle(
        "turn_no_candidate01",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
    )

    registry.begin_native_attempt(
        backend="claude",
        process_scope="scope-turn_canceled01",
        turn_id="turn_canceled01",
        requested_model_id="scenario-model",
        source_id="src_outcome01",
        resolved_model_id="scenario-model",
        via_mapping=False,
    )
    registry.settle(
        "turn_canceled01",
        settled_by=SETTLED_BY_STOPPED,
    )

    for turn_id in (
        "turn_served01",
        "turn_exhausted01",
        "turn_failed01",
        "turn_drop01",
        "turn_no_candidate01",
        "turn_canceled01",
    ):
        record = store.get(turn_id)
        assert record is not None
        Draft7Validator(schema).validate(record)
        records.append(record)

    assert {record["outcome"] for record in records} == set(schema["properties"]["outcome"]["enum"])
    canceled = store.get("turn_canceled01")
    dropped = store.get("turn_drop01")
    assert canceled is not None and dropped is not None
    assert canceled["canceled_attempt"] is not None
    assert "reason" not in canceled["canceled_attempt"]
    assert dropped["outcome"] != canceled["outcome"]
    assert dropped["terminal_error"]["reason"] == "stream_interrupted"

    ambiguous_store = BoundedProvenanceStore(tmp_path / "ambiguous.json")
    ambiguous = TurnCorrelationRegistry(ambiguous_store)
    for turn_id in ("turn_cancel_a", "turn_cancel_b"):
        ambiguous.begin_native_attempt(
            backend="claude",
            process_scope="shared-cancel-scope",
            turn_id=turn_id,
            requested_model_id="scenario-model",
            source_id="src_outcome01",
            resolved_model_id="scenario-model",
            via_mapping=False,
        )
    for turn_id in ("turn_cancel_a", "turn_cancel_b"):
        ambiguous.settle(turn_id, settled_by=SETTLED_BY_STOPPED)
        assert ambiguous_store.get(turn_id) is None

    ambiguous.begin_native_attempt(
        backend="claude",
        process_scope="sequential-cancel-scope",
        turn_id="turn_cancel_control",
        requested_model_id="scenario-model",
        source_id="src_outcome01",
        resolved_model_id="scenario-model",
        via_mapping=False,
    )
    ambiguous.settle("turn_cancel_control", settled_by=SETTLED_BY_STOPPED)
    control = ambiguous_store.get("turn_cancel_control")
    assert control is not None
    Draft7Validator(schema).validate(control)
    assert control["outcome"] == canceled["outcome"]


def test_mh_turn_copy_keys_cover_the_authoritative_outcome_matrix() -> None:
    """MH-TURN-COPY-001: every frozen turn decision resolves to its backend locale copy or silence."""

    rows = _turn_copy_rows()
    api_contract = (CONTRACTS / "api.md").read_text()
    api_decisions = set(re.findall(r"\bturn\.[a-z_.]+", api_contract))
    schema_outcomes = set(_provenance_schema()["properties"]["outcome"]["enum"])
    assert {row["decision"] for row in rows} == api_decisions
    assert {row["outcome"] for row in rows} == schema_outcomes

    projected = {
        (decision, variant): copy.key if copy is not None else None
        for decision, rule in TURN_OUTCOME_RENDERING_AUTHORITY.items()
        for variant, _expected_key in rule.copy_keys
        for copy in (
            project_turn_outcome_copy(
                _projection_for_copy_variant(decision, variant)
            ),
        )
    }
    expected = {
        (decision, variant): key
        for decision, rule in TURN_OUTCOME_RENDERING_AUTHORITY.items()
        for variant, key in rule.copy_keys
    }
    assert projected == expected
    copy_keys = {key for key in expected.values() if key is not None}

    for language in ("en", "zh"):
        locale = json.loads(Path(f"vibe/i18n/{language}.json").read_text())
        assert all(isinstance(_locale_value(locale, key), str) for key in copy_keys)

    nonfallback = TURN_OUTCOME_RENDERING_AUTHORITY["turn.request_nonfallback"]
    assert "modelHub.launch.retry" not in {
        key for _variant, key in nonfallback.copy_keys
    }
