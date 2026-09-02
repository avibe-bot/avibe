"""First-wave Model Hub scenarios for the v3 configured-chain contract."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from config.v2_config import (
    ModelHubBackendModelConfig,
    ModelHubConfig,
    ModelHubRouteConfig,
    ModelHubRouteHopConfig,
)
from core.handlers.model_hub.adapter import (
    SOURCE_PROTOCOLS,
    ObservationDiscovery,
    ObservationOutcome,
    SourceObservation,
)
from core.handlers.model_hub.service import (
    ModelHubError,
    _matching_v1_model_id as matching_v1_model_id,
)
from modules.agents.model_hub import resolve_model_hub_turn
from tests.scenario_harness.model_hub import (
    MemoryModelHubStore,
    ModelHubScenarioAdapter,
    config_with_sources,
    fixed_model,
    round_trip,
    service_for,
    source,
    source_model,
)


@pytest.fixture(autouse=True)
def _enable_model_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIBE_MODEL_HUB_ENABLED", "1")


def _route_pairs(
    config: ModelHubConfig,
    backend: str,
    menu_model: str,
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (hop.source_id, hop.model_id)
        for hop in config.agents[backend].routes[menu_model].hops
    )


def test_mh_s1_001_runtime_resolution_keeps_the_persisted_hop_identity() -> None:
    """MH-S1-001: live health changes annotate exact hops but never choose new ones."""

    menu_model = fixed_model("claude")
    first = source("src_first001", ["upstream-first"])
    second = source("src_second01", ["upstream-second"])
    config = config_with_sources(
        [first, second],
        backend="claude",
        menu_model=menu_model,
        hops=[(first.id, "upstream-first"), (second.id, "upstream-second")],
    )
    before = resolve_model_hub_turn(
        config,
        "claude",
        menu_model,
        now=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    changed = round_trip(config)
    changed.sources[0].state.status = "needs_action"
    changed.sources[0].state.detail_key = (
        "models.source.needs_action.credential_revoked"
    )
    changed.sources[1].models.append(source_model("catalog-only-new-model"))
    after = resolve_model_hub_turn(
        changed,
        "claude",
        menu_model,
        now=datetime(2030, 8, 1, tzinfo=timezone.utc),
    )

    assert (
        before.source_model_ids
        == after.source_model_ids
        == _route_pairs(config, "claude", menu_model)
    )
    assert tuple(
        (item.source_id, item.model_id) for item in after.inspected_hops
    ) == before.source_model_ids
    assert {item.id for item in after.candidates} <= {
        source_id for source_id, _ in before.source_model_ids
    }
    assert after.target_model in {
        model_id for _, model_id in before.source_model_ids
    }


def test_mh_config_001_chain_is_the_persisted_artifact_until_explicit_edit(
    tmp_path: Path,
) -> None:
    """MH-CONFIG-001: refresh, reload, and inventory changes preserve a chain until a route edit."""

    menu_model = fixed_model("claude")
    first = source("src_chain001", [menu_model])
    second = source("src_chain002", [menu_model])
    config = config_with_sources(
        [first, second],
        backend="claude",
        menu_model=menu_model,
        hops=[(first.id, menu_model), (second.id, menu_model)],
    )
    store = MemoryModelHubStore(config)
    adapter = ModelHubScenarioAdapter(
        discovery_models=(menu_model,),
        refresh_models=(menu_model, "new-inventory-model"),
    )
    service = service_for(tmp_path, store, adapter)
    original = _route_pairs(store.load(), "claude", menu_model)

    asyncio.run(service.refresh_source(first.id))
    assert _route_pairs(store.load(), "claude", menu_model) == original
    assert _route_pairs(round_trip(store.load()), "claude", menu_model) == original

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
    assert [
        (hop["source_id"], hop["model_id"]) for hop in result["chain"]["chain"]
    ] == [(second.id, menu_model), (first.id, menu_model)]
    assert _route_pairs(round_trip(store.load()), "claude", menu_model) == (
        (second.id, menu_model),
        (first.id, menu_model),
    )
    assert adapter.observation_calls == []
    assert adapter.discovery_calls == [
        (first.vendor, first.protocol, first.base_url, first.credential_ref)
    ]


def test_mh_match_001_add_source_persists_and_reports_each_exact_position(
    tmp_path: Path,
) -> None:
    """MH-MATCH-001: add-time matching writes exact hops and returns their persisted positions."""

    menu_model = fixed_model("claude")
    existing = source("src_existing01", [menu_model])
    config = config_with_sources(
        [existing],
        backend="claude",
        menu_model=menu_model,
        hops=[(existing.id, menu_model)],
    )
    store = MemoryModelHubStore(config)
    adapter = ModelHubScenarioAdapter(
        observation=SourceObservation(
            outcome=ObservationOutcome.OBSERVED,
            reachable=True,
            authenticated=True,
            protocol="anthropic",
            discovery=ObservationDiscovery.SUCCEEDED,
            model_ids=(menu_model,),
        ),
        refresh_models=(menu_model,),
    )
    service = service_for(tmp_path, store, adapter)

    result = asyncio.run(
        service.create_source(
            {
                "kind": "api_key",
                "vendor": "anthropic",
                "display_name": "added-source",
                "base_url": "https://upstream.example/v1",
                "key": "sk-scenario-add-key",
            }
        )
    )
    added_id = result["source"]["id"]
    route = store.load().agents["claude"].routes[menu_model]
    positions = {
        (hop.source_id, hop.model_id): index
        for index, hop in enumerate(route.hops, start=1)
    }
    returned = {
        (item["source_id"], item["model_id"]): item["position"]
        for item in result["added_to"]
    }

    assert returned
    assert all(item["source_id"] == added_id for item in result["added_to"])
    assert returned == {pair: positions[pair] for pair in returned}
    assert result["adopted_by"] == [
        {"backend": "claude", "menu_model": menu_model}
    ]
    assert store.load().agents["claude"].sources.order[-1] == added_id
    assert adapter.revoked == adapter.provisioned_transient

    persisted_after_add = _route_pairs(store.load(), "claude", menu_model)
    asyncio.run(service.refresh_source(added_id))
    restarted = service_for(tmp_path / "restart", store, adapter)
    assert _route_pairs(store.load(), "claude", menu_model) == persisted_after_add
    assert (
        _route_pairs(round_trip(restarted.store.load()), "claude", menu_model)
        == persisted_after_add
    )
    assert len(adapter.observation_calls) == 1


def test_mh_match_002_matching_tie_break_is_independent_of_inventory_order() -> None:
    """MH-MATCH-002: the matching-v1 total order produces one result for either processing order."""

    menu_model = "claude-opus-4-6"
    candidates = (
        "claude-opus-4-6-20260101",
        "claude-opus-4-6-20260115",
    )
    first = source(
        "src_tieorder1",
        candidates,
        vendor="anthropic",
        kind="subscription",
        channel="native_cli",
        credential_ref=None,
    )
    second = source(
        "src_tieorder2",
        reversed(candidates),
        vendor="anthropic",
        kind="subscription",
        channel="native_cli",
        credential_ref=None,
    )

    def matches(source_order):
        return {
            item.id: matching_v1_model_id(
                backend="claude",
                requested_model=menu_model,
                source=item,
            )
            for item in source_order
        }

    first_order = matches((first, second))
    second_order = matches((second, first))

    assert first_order == second_order
    assert set(first_order.values()) == {"claude-opus-4-6-20260115"}


def test_mh_source_delete_001_removes_every_reference_and_preserves_survivor_order(
    tmp_path: Path,
) -> None:
    """MH-SRC-DELETE-001: source deletion is one transaction across all backend orders and chains."""

    claude_model = fixed_model("claude")
    codex_model = fixed_model("codex")
    opencode_model = "custom/route-model"
    models = (claude_model, codex_model, "route-model")
    first = source("src_delete01", models)
    doomed = source("src_delete02", models)
    last = source("src_delete03", models)
    config = config_with_sources(
        [first, doomed, last],
        backend="claude",
        menu_model=claude_model,
        hops=[
            (first.id, claude_model),
            (doomed.id, claude_model),
            (last.id, claude_model),
        ],
    )
    for backend, menu_model, upstream_model in (
        ("codex", codex_model, codex_model),
        ("opencode", opencode_model, "route-model"),
    ):
        agent = config.agents[backend]
        if backend == "opencode":
            agent.menu.checked = [opencode_model]
            agent.models = [ModelHubBackendModelConfig(id=opencode_model)]
        agent.routes[menu_model] = ModelHubRouteConfig(
            hops=tuple(
                ModelHubRouteHopConfig(item.id, upstream_model)
                for item in (first, doomed, last)
            )
        )
    store = MemoryModelHubStore(config)
    adapter = ModelHubScenarioAdapter()
    service = service_for(tmp_path, store, adapter)

    with pytest.raises(ModelHubError) as refusal:
        asyncio.run(service.delete_source(doomed.id))
    assert refusal.value.code == "source_in_route_chain"

    result = asyncio.run(
        service.delete_source(
            doomed.id,
            force=True,
            confirmed_remove_hops=refusal.value.data["would_remove_hops"],
            confirmed_interruptions=refusal.value.data["would_interrupt"],
        )
    )
    assert result["removed_hops"]
    assert doomed.id not in {item.id for item in store.load().sources}
    for backend, menu_model in (
        ("claude", claude_model),
        ("codex", codex_model),
        ("opencode", opencode_model),
    ):
        assert doomed.id not in store.load().agents[backend].sources.order
        assert [
            hop.source_id
            for hop in store.load().agents[backend].routes[menu_model].hops
        ] == [first.id, last.id]
    assert round_trip(store.load()).to_payload() == store.load().to_payload()
    assert adapter.revoked == [doomed.credential_ref]


def test_mh_supply_gap_001_empty_chain_remains_visible_after_inventory_loss(
    tmp_path: Path,
) -> None:
    """MH-SUPPLY-GAP-001: a now-unsupplied menu model stays visible with a zero-length Route."""

    menu_model = fixed_model("claude")
    supplied = source("src_supply01", [menu_model])
    store = MemoryModelHubStore(
        config_with_sources(
            [supplied],
            backend="claude",
            menu_model=menu_model,
            hops=[(supplied.id, menu_model)],
        )
    )
    adapter = ModelHubScenarioAdapter(
        discovery_models=(menu_model,),
        refresh_models=("replacement-only-model",),
    )
    service = service_for(tmp_path, store, adapter)

    with pytest.raises(ModelHubError) as refusal:
        asyncio.run(service.refresh_source(supplied.id))
    result = asyncio.run(
        service.refresh_source(
            supplied.id,
            force=True,
            confirmed_remove_hops=refusal.value.data["would_remove_hops"],
            confirmed_interruptions=refusal.value.data["would_interrupt"],
        )
    )
    assert result["removed_hops"] == [
        {
            "backend": "claude",
            "menu_model": menu_model,
            "source_id": supplied.id,
            "model_id": menu_model,
            "position": 1,
        }
    ]
    model_supply = next(
        row
        for row in service.list_agents()[0]["model_supply"]
        if row["model_id"] == menu_model
    )
    assert model_supply["chain_length"] == 0
    assert service.agent_chain("claude", menu_model)["chain"] == []


def test_mh_protocol_001_saved_protocol_is_response_observed_and_transient_state_is_cleaned(
    tmp_path: Path,
) -> None:
    """MH-PROTOCOL-001: observation proves protocol before persistence and leaves no transient ref."""

    store = MemoryModelHubStore(config_with_sources([]))
    adapter = ModelHubScenarioAdapter(
        observation=SourceObservation(
            outcome=ObservationOutcome.OBSERVED,
            reachable=True,
            authenticated=True,
            protocol="openai_chat",
            discovery=ObservationDiscovery.SUCCEEDED,
            model_ids=("observed-model",),
        )
    )
    service = service_for(tmp_path, store, adapter)
    observed = asyncio.run(
        service.observe_source(
            {
                "vendor": "custom",
                "base_url": "https://upstream.example/v1",
                "key": "sk-scenario-observe-key",
            }
        )
    )

    assert observed["observation"]["protocol"] in SOURCE_PROTOCOLS
    assert observed["observation"]["authenticated"] == "authenticated"
    assert store.load().sources == []
    assert adapter.revoked == adapter.provisioned_transient
    assert all("credential_ref" not in value for value in observed.values())

    ambiguous_store = MemoryModelHubStore(config_with_sources([]))
    ambiguous_adapter = ModelHubScenarioAdapter(
        observation=SourceObservation(
            outcome=ObservationOutcome.AMBIGUOUS,
            reachable=True,
            authenticated=True,
            protocol=None,
            discovery=ObservationDiscovery.NOT_ATTEMPTED,
            model_ids=(),
        )
    )
    ambiguous_service = service_for(
        tmp_path / "ambiguous",
        ambiguous_store,
        ambiguous_adapter,
    )
    with pytest.raises(ModelHubError) as failure:
        asyncio.run(
            ambiguous_service.create_source(
                {
                    "kind": "api_key",
                    "vendor": "custom",
                    "display_name": "ambiguous-source",
                    "base_url": "https://ambiguous.example/v1",
                    "key": "sk-scenario-ambiguous-key",
                }
            )
        )
    assert failure.value.code == "discovery_failed"
    assert ambiguous_store.load().sources == []
    assert ambiguous_adapter.revoked == ambiguous_adapter.provisioned_transient


def test_mh_protocol_002_contract_exposes_only_the_three_authoritative_transports() -> None:
    """MH-PROTOCOL-002: the schema and adapter authority have one exact protocol vocabulary."""

    schema = json.loads(
        Path("docs/plans/model-hub-contracts/source.schema.json").read_text()
    )
    schema_protocols = tuple(schema["properties"]["protocol"]["enum"])
    assert schema_protocols == SOURCE_PROTOCOLS
    assert len(schema_protocols) == len(set(schema_protocols))


def test_mh_protocol_003_manual_selection_requires_matching_response_proof(
    tmp_path: Path,
) -> None:
    """MH-PROTOCOL-003: manual selection probes one type and never overrides evidence."""

    store = MemoryModelHubStore(config_with_sources([]))
    adapter = ModelHubScenarioAdapter(
        observation=SourceObservation(
            outcome=ObservationOutcome.OBSERVED,
            reachable=True,
            authenticated=True,
            protocol="openai_responses",
            discovery=ObservationDiscovery.SUCCEEDED,
            model_ids=("selected-model",),
        )
    )
    service = service_for(tmp_path, store, adapter)

    created = asyncio.run(
        service.create_source(
            {
                "kind": "api_key",
                "vendor": "custom",
                "base_url": "https://relay.example/v1",
                "key": "sk-scenario-manual-protocol",
                "protocol": "openai_responses",
            }
        )
    )

    assert created["source"]["protocol"] == "openai_responses"
    assert adapter.observation_calls == [
        ("custom", "https://relay.example/v1", ("openai_responses",))
    ]
    assert adapter.revoked == adapter.provisioned_transient

    mismatch_store = MemoryModelHubStore(config_with_sources([]))
    mismatch_adapter = ModelHubScenarioAdapter(
        observation=SourceObservation(
            outcome=ObservationOutcome.OBSERVED,
            reachable=True,
            authenticated=True,
            protocol="openai_chat",
            discovery=ObservationDiscovery.SUCCEEDED,
            model_ids=("wrong-protocol-model",),
        )
    )
    mismatch_service = service_for(
        tmp_path / "mismatch",
        mismatch_store,
        mismatch_adapter,
    )
    with pytest.raises(ModelHubError) as mismatch:
        asyncio.run(
            mismatch_service.create_source(
                {
                    "kind": "api_key",
                    "vendor": "custom",
                    "base_url": "https://relay.example/v1",
                    "key": "sk-scenario-mismatched-protocol",
                    "protocol": "openai_responses",
                }
            )
        )
    assert mismatch.value.status == 502
    assert mismatch_store.load().sources == []
    assert mismatch_adapter.revoked == mismatch_adapter.provisioned_transient

    ambiguous_store = MemoryModelHubStore(config_with_sources([]))
    ambiguous_adapter = ModelHubScenarioAdapter(
        observation=SourceObservation(
            outcome=ObservationOutcome.AMBIGUOUS,
            reachable=True,
            authenticated=True,
            protocol=None,
            discovery=ObservationDiscovery.NOT_ATTEMPTED,
            model_ids=(),
        )
    )
    ambiguous_service = service_for(
        tmp_path / "manual-ambiguous",
        ambiguous_store,
        ambiguous_adapter,
    )
    with pytest.raises(ModelHubError) as ambiguous:
        asyncio.run(
            ambiguous_service.create_source(
                {
                    "kind": "api_key",
                    "vendor": "custom",
                    "display_name": "still-unproven",
                    "base_url": "https://relay.example/v1",
                    "key": "sk-scenario-unproven-manual",
                    "protocol": "openai_responses",
                }
            )
        )
    assert ambiguous.value.code == "discovery_failed"
    assert ambiguous_store.load().sources == []
    assert ambiguous_adapter.revoked == ambiguous_adapter.provisioned_transient


def test_mh_ac29_001_persisted_source_payload_round_trips_through_the_canonical_validator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """MH-AC29-001: a real migration write remains valid after serialization and reload."""

    native_home = tmp_path / "native-home"
    claude_config = native_home / ".claude"
    claude_config.mkdir(parents=True)
    (claude_config / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_API_KEY": "sk-scenario-ac29-key"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_config))
    monkeypatch.setenv("CODEX_HOME", str(native_home / ".codex"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(native_home / ".config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(native_home / ".cache"))
    monkeypatch.setattr(Path, "home", lambda: native_home)

    menu_model = fixed_model("claude")
    store = MemoryModelHubStore(config_with_sources([]))
    adapter = ModelHubScenarioAdapter(
        observation=SourceObservation(
            outcome=ObservationOutcome.OBSERVED,
            reachable=True,
            authenticated=True,
            protocol="anthropic",
            discovery=ObservationDiscovery.SUCCEEDED,
            model_ids=(menu_model,),
        )
    )
    service = service_for(tmp_path, store, adapter)
    scan = service.migration_scan()["items"]
    result = asyncio.run(service.migration_apply([item["id"] for item in scan]))

    serialized = json.loads(json.dumps(store.load().to_payload()))
    reloaded = ModelHubConfig.from_payload(serialized)
    persisted_sources = {item.id: item.to_payload() for item in reloaded.sources}
    response_sources = {}
    for item in result["sources"]:
        persisted = json.loads(
            json.dumps({key: value for key, value in item.items() if key != "adopted_by"})
        )
        for model in persisted["models"]:
            if model.get("retired") is False:
                model.pop("retired")
        response_sources[persisted["id"]] = persisted

    assert result["applied"] == len(scan)
    assert persisted_sources == response_sources
    assert store.saved_payloads
    assert reloaded.to_payload() == store.load().to_payload()
    assert set(_route_pairs(reloaded, "claude", menu_model)) == {
        (position["source_id"], position["model_id"])
        for position in result["added_to"]
        if position["backend"] == "claude"
        and position["menu_model"] == menu_model
    }
