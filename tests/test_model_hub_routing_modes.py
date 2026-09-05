from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from config.v2_config import (
    ModelHubBackendModelConfig,
    ModelHubConfig,
    ModelHubModelConfig,
    ModelHubRouteConfig,
    ModelHubRouteHopConfig,
    ModelHubSourceStateConfig,
)
from core.handlers.model_hub.adapter import RawCallOutcome, RawOutcomeKind, SourceBinding
from core.handlers.model_hub.identifiers import MODEL_ID_MAX_LENGTH
from core.handlers.model_hub.resolver import effective_model_route, resolve_model_hub_turn
from core.handlers.model_hub.service import ModelHubError
from scripts.check_model_hub_authorities import AuthorityInput, _typescript_string_union
from tests.test_model_hub_resolution import _service, _source
from vibe.model_hub_runtime.config import _append_source
from vibe.model_hub_runtime.state import EngineStateStore, SourceRecord


MODEL = "claude-opus-4-6"


def _sparse_config(*sources):
    config = ModelHubConfig(sources=list(sources))
    agent = config.agents["claude"]
    agent.mode = "hub"
    agent.models = [ModelHubBackendModelConfig(id=MODEL)]
    agent.sources.order = [source.id for source in sources]
    return config


def _pairs(plan):
    return [(hop.source_id, hop.model_id) for hop in plan.hops]


def test_sparse_matching_tier_excludes_speculation_and_survives_health_changes():
    absent = _source("src_absent001", ())
    known = _source("src_known001")
    config = _sparse_config(absent, known)
    plan = effective_model_route(config, "claude", MODEL)
    assert plan.manual_override is None
    assert plan.route_origin == "automatic"
    assert _pairs(plan) == [(known.id, MODEL)]

    known.state = ModelHubSourceStateConfig(
        status="needs_action", detail_key="models.source.needs_action.oauth_expired"
    )
    blocked = resolve_model_hub_turn(config, "claude", MODEL)
    assert blocked.route_origin == "automatic"
    assert blocked.candidates == ()
    assert [hop.source_id for hop in blocked.inspected_hops] == [known.id]
    assert effective_model_route(config, "claude", MODEL) == plan
    assert config.agents["claude"].routes == {}


def test_no_matching_inventory_forwards_exact_id_to_all_defaults():
    first = _source("src_first001", ())
    second = _source("src_second001", ("other-model",))
    config = _sparse_config(first, second)
    plan = effective_model_route(config, "claude", MODEL)
    assert plan.route_origin == "passthrough"
    assert _pairs(plan) == [(first.id, MODEL), (second.id, MODEL)]
    resolution = resolve_model_hub_turn(config, "claude", MODEL)
    assert resolution.source is first
    assert resolution.target_model == MODEL
    assert all(not hop.inventory_member and hop.runnable for hop in resolution.inspected_hops)


def test_manual_inventory_is_matching_evidence_and_retirement_is_not():
    first = _source("src_first001", ())
    second = _source("src_second001", ())
    second.models = [ModelHubModelConfig(id=MODEL, provenance="manual")]
    config = _sparse_config(first, second)
    assert _pairs(effective_model_route(config, "claude", MODEL)) == [(second.id, MODEL)]
    second.models = [ModelHubModelConfig(id=MODEL, provenance="discovered", retired=True)]
    plan = effective_model_route(config, "claude", MODEL)
    assert plan.route_origin == "passthrough"
    assert _pairs(plan) == [(first.id, MODEL)]
    config.agents["claude"].routes[MODEL] = ModelHubRouteConfig(hops=(ModelHubRouteHopConfig(second.id, MODEL),))
    blocked = resolve_model_hub_turn(config, "claude", MODEL)
    assert blocked.route_origin == "manual"
    assert not blocked.candidates
    assert blocked.inspected_hops[0].reason == "model_unsupported"


def test_native_alias_tier_is_chosen_before_hub_transport_filter():
    native = _source("src_native001", (), kind="subscription", channel="native_cli", credential_ref=None)
    native.models = [ModelHubModelConfig(id="claude-opus-4-6-20260115", provenance="manual")]
    api = _source("src_api00001", ())
    config = _sparse_config(api, native)
    plan = effective_model_route(config, "claude", MODEL)
    assert plan.route_origin == "automatic"
    assert _pairs(plan) == [(native.id, "claude-opus-4-6-20260115")]
    resolution = resolve_model_hub_turn(config, "claude", MODEL, supply_channel="hub")
    assert resolution.route_origin == "automatic"
    assert not resolution.candidates
    assert [hop.source_id for hop in resolution.inspected_hops] == [native.id]


def test_absence_does_not_broaden_backend_model_admission():
    config = _sparse_config(_source("src_first001", ()))
    assert effective_model_route(config, "claude", "not-in-catalog").hops == ()
    assert resolve_model_hub_turn(config, "claude", "not-in-catalog").source is None


@pytest.mark.parametrize("channel", ["hub", "native_cli"])
def test_subscriptions_do_not_gain_unknown_fallback_or_manual_admission(tmp_path, channel):
    source = _source(
        "src_subscription01",
        (),
        kind="subscription",
        channel=channel,
        credential_ref=None if channel == "native_cli" else "cred_source",
    )
    config = _sparse_config(source)
    service, store, _ = _service(tmp_path, config)
    plan = effective_model_route(config, "claude", MODEL)
    assert plan.hops == ()
    assert plan.route_origin is None
    payload = {"hops": [{"source_id": source.id, "model_id": "unknown-target"}]}
    with pytest.raises(ModelHubError):
        asyncio.run(service.set_agent_chain("claude", MODEL, payload))
    with pytest.raises(ModelHubError):
        service.preview_agent_chain("claude", MODEL, {"manual_override": payload})
    assert service._bindings(config) == []

    # Historical stale intent remains editable, but never becomes runnable.
    config.agents["claude"].routes[MODEL] = ModelHubRouteConfig(
        hops=(ModelHubRouteHopConfig(source.id, "unknown-target"),)
    )
    saved = asyncio.run(service.set_agent_chain("claude", MODEL, payload))
    assert saved["chain"]["manual_override"] == payload
    assert saved["chain"]["route_origin"] == "manual"
    assert saved["chain"]["current"] is None
    assert saved["chain"]["chain"][0]["reason"] == "model_unsupported"
    assert store.config.agents["claude"].routes[MODEL].to_payload() == payload


def test_subscription_match_suppresses_unknown_api_speculation(tmp_path):
    api = _source("src_api00001", ())
    subscription = _source("src_subscription01", kind="subscription")
    config = _sparse_config(api, subscription)
    service, _, _ = _service(tmp_path, config)
    plan = effective_model_route(config, "claude", MODEL)
    assert plan.route_origin == "automatic"
    assert _pairs(plan) == [(subscription.id, MODEL)]
    bindings = service._bindings(config)
    assert all(binding.route_model_ids == () for binding in bindings)


def test_mixed_unmatched_defaults_only_use_api_key_passthrough():
    subscription = _source("src_subscription01", (), kind="subscription")
    api = _source("src_api00001", ())
    config = _sparse_config(subscription, api)
    plan = effective_model_route(config, "claude", MODEL)
    assert plan.route_origin == "passthrough"
    assert _pairs(plan) == [(api.id, MODEL)]


def test_equal_save_has_manual_provenance_and_empty_override_survives_changes(tmp_path):
    source = _source("src_first001")
    config = _sparse_config(source)
    service, store, adapter = _service(tmp_path, config)
    hops = [{"source_id": source.id, "model_id": MODEL}]
    saved = asyncio.run(service.set_agent_chain("claude", MODEL, {"hops": hops}))
    assert saved["chain"]["route_origin"] == "manual"
    assert saved["chain"]["manual_override"] == {"hops": hops}
    with pytest.raises(ModelHubError) as guard:
        asyncio.run(service.set_agent_chain("claude", MODEL, {"hops": []}))
    empty = asyncio.run(service.set_agent_chain("claude", MODEL, {"hops": [], "force": True, **guard.value.data}))
    assert empty["chain"]["manual_override"] == {"hops": []}
    assert empty["chain"]["route_origin"] is None
    asyncio.run(service.set_agent_sources("claude", {"order": []}))
    store.config.sources[0].models.clear()
    reloaded = ModelHubConfig.from_payload(store.config.to_payload())
    assert reloaded.agents["claude"].routes[MODEL].hops == ()
    assert effective_model_route(reloaded, "claude", MODEL).hops == ()
    assert adapter.synced


def test_preview_and_restore_use_same_plan_without_preview_side_effects(tmp_path):
    source = _source("src_first001", ())
    config = _sparse_config(source)
    config.agents["claude"].routes[MODEL] = ModelHubRouteConfig()
    service, store, adapter = _service(tmp_path, config)
    before = store.config.to_payload()
    writes = []
    original_save = store.save
    store.save = lambda updated: (writes.append(updated), original_save(updated))[-1]
    draft = service.preview_agent_chain("claude", MODEL, {"manual_override": None})
    assert draft["manual_override"] is None
    assert draft["route_origin"] == "passthrough"
    assert draft["current"] == {"source_id": source.id, "model_id": MODEL}
    assert store.config.to_payload() == before
    assert writes == adapter.synced == []
    assert not (tmp_path / "events.json").exists()
    restored = asyncio.run(service.delete_agent_chain("claude", MODEL))
    assert restored["chain"] == draft
    assert MODEL not in store.config.agents["claude"].routes
    repeated = asyncio.run(service.delete_agent_chain("claude", MODEL))
    assert repeated == restored
    assert len(adapter.synced) == 1


def test_restore_guards_effective_removal_and_accepts_only_exact_echo(tmp_path):
    default = _source("src_default01")
    manual = _source("src_manual001", ())
    config = _sparse_config(default)
    config.sources.append(manual)
    config.agents["claude"].routes[MODEL] = ModelHubRouteConfig(
        hops=(ModelHubRouteHopConfig(manual.id, "exact-unlisted"),)
    )
    service, store, _ = _service(tmp_path, config)
    with pytest.raises(ModelHubError) as refusal:
        asyncio.run(service.delete_agent_chain("claude", MODEL))
    assert refusal.value.code == "source_in_route_chain"
    assert refusal.value.data["would_interrupt"] == []
    assert refusal.value.data["would_remove_hops"][0]["model_id"] == "exact-unlisted"
    with pytest.raises(ModelHubError):
        asyncio.run(service.delete_agent_chain("claude", MODEL, {"force": True}))
    result = asyncio.run(service.delete_agent_chain("claude", MODEL, {"force": True, **refusal.value.data}))
    assert result["chain"]["route_origin"] == "automatic"
    assert result["chain"]["current"] == {"source_id": default.id, "model_id": MODEL}
    assert MODEL not in store.config.agents["claude"].routes


def test_default_membership_guards_generated_removal_without_rewriting_manual(tmp_path):
    first = _source("src_first001")
    second = _source("src_second001")
    config = _sparse_config(first, second)
    service, store, adapter = _service(tmp_path, config)
    asyncio.run(service.set_agent_sources("claude", {"order": [second.id, first.id]}))
    assert adapter.synced == []
    assert service.agent_chain("claude", MODEL)["chain"][0]["source_id"] == second.id
    with pytest.raises(ModelHubError) as refusal:
        asyncio.run(service.set_agent_sources("claude", {"order": [first.id]}))
    assert refusal.value.data["would_remove_hops"][0]["source_id"] == second.id
    assert refusal.value.data["would_interrupt"] == []
    asyncio.run(service.set_agent_sources("claude", {"order": [first.id], "force": True, **refusal.value.data}))
    asyncio.run(service.set_agent_chain("claude", MODEL, {"hops": [{"source_id": second.id, "model_id": "exact"}]}))
    original = store.config.agents["claude"].routes[MODEL].to_payload()
    asyncio.run(service.set_agent_sources("claude", {"order": []}))
    assert store.config.agents["claude"].routes[MODEL].to_payload() == original
    assert service.agent_chain("claude", MODEL)["current"] == {"source_id": second.id, "model_id": "exact"}


def test_engine_route_targets_are_deterministic_truthful_and_health_mode_independent(tmp_path):
    source = _source("src_first001", ("inventory-only",))
    config = _sparse_config(source)
    agent = config.agents["claude"]
    agent.models.append(ModelHubBackendModelConfig(id="claude-extra"))
    agent.routes["claude-extra"] = ModelHubRouteConfig(hops=(ModelHubRouteHopConfig(source.id, "unlisted"),))
    service, _, _ = _service(tmp_path, config)
    binding = service._bindings(config)[0]
    assert binding.model_ids == ("inventory-only",)
    assert binding.route_model_ids == (MODEL, "unlisted")
    agent.mode = "direct"
    source.state = ModelHubSourceStateConfig(
        status="needs_action", detail_key="models.source.needs_action.oauth_expired"
    )
    assert service._bindings(config) == [binding]
    source.models.clear()
    empty_inventory = service._bindings(config)[0]
    assert empty_inventory.model_ids == ()
    assert empty_inventory.route_model_ids == binding.route_model_ids
    assert source.models == []


@pytest.mark.parametrize(
    "protocol,section",
    [("anthropic", "claude-api-key"), ("openai_responses", "codex-api-key"), ("openai_chat", "openai-compatibility")],
)
def test_engine_compiler_registers_inventory_union_without_invented_reasoning(tmp_path, protocol, section):
    store = EngineStateStore(tmp_path / "engine")
    ref = store.store_api_key("fixture-key", vendor="custom", protocol=protocol, base_url="https://api.example/v1")
    binding = SourceBinding(
        source_id="src_first001",
        vendor="custom",
        protocol=protocol,
        base_url="https://api.example/v1",
        credential_ref=ref,
        allowed_origins=(),
        model_ids=("listed",),
        model_reasoning_efforts=(("listed", ("low", "high")),),
        route_model_ids=("listed", "unlisted"),
    )
    store.sync_sources([binding])
    record = store.list_sources()[0]
    assert record.model_ids == ("listed",)
    assert record.route_model_ids == ("listed", "unlisted")
    payload = {}
    _append_source(payload, record, store)
    assert payload[section][0]["models"] == [
        {"name": "listed", "alias": "listed", "thinking": {"levels": ["high", "low"]}},
        {"name": "unlisted", "alias": "unlisted"},
    ]
    store.sync_sources([replace(binding, model_ids=(), model_reasoning_efforts=())])
    assert store.list_sources()[0].model_ids == ()


def test_internal_engine_record_loads_old_shape_without_route_targets():
    record = SourceRecord(
        source_id="src_first001",
        vendor="custom",
        protocol="openai_chat",
        base_url="https://api.example/v1",
        credential_ref="cred_fixture",
        allowed_origins=(),
        model_ids=(),
        prefix="avibe-fixture",
    )
    payload = asdict(record)
    payload["model_ids"] = []
    payload["model_reasoning_efforts"] = []
    payload.pop("route_model_ids")
    assert SourceRecord.from_payload(payload).route_model_ids == ()


@pytest.mark.parametrize(
    "declaration",
    [
        'export type RouteOrigin = "automatic" | "manual" | "passthrough" | null;',
        "export type RouteOrigin =\n  | 'automatic'\n  | 'manual'\n  | 'passthrough'\n  | null;",
    ],
)
def test_authority_extractor_reads_exported_nullable_string_union(monkeypatch, declaration):
    source = AuthorityInput(Path.cwd())
    monkeypatch.setattr(source, "text", lambda _: declaration)
    assert _typescript_string_union(source, {"file": "fixture.ts", "name": "RouteOrigin"}) == {
        "automatic",
        "manual",
        "passthrough",
    }


@pytest.mark.parametrize(
    "declaration",
    [
        'type RouteOrigin = "manual";',
        'export type RouteOrigin = "manual" | string;',
        'export type RouteOrigin = "manual" |;',
        "export type RouteOrigin = null;",
    ],
)
def test_authority_extractor_rejects_missing_or_nonliteral_union(monkeypatch, declaration):
    source = AuthorityInput(Path.cwd())
    monkeypatch.setattr(source, "text", lambda _: declaration)
    with pytest.raises(ValueError):
        _typescript_string_union(source, {"file": "fixture.ts", "name": "RouteOrigin"})


def _loaded_catalog_config(backend, requested, *sources):
    config = ModelHubConfig(sources=list(sources))
    agent = config.agents[backend]
    agent.mode = "hub"
    agent.models = [
        ModelHubBackendModelConfig(
            id=requested,
            origin="manual",
            native_protocol="openai_responses" if backend == "opencode" else None,
        )
    ]
    agent.sources.order = [source.id for source in sources]
    return ModelHubConfig.from_payload(config.to_payload())


@pytest.mark.parametrize("backend", ["claude", "codex"])
@pytest.mark.parametrize("state", ["automatic", "manual", "empty"])
@pytest.mark.parametrize("operation", ["put", "preview", "restore_preview", "restore_save"])
def test_persisted_legacy_catalog_identity_survives_route_consumers(tmp_path, backend, state, operation):
    requested = "legacy-" + "x" * MODEL_ID_MAX_LENGTH
    source = _source("src_legacy001", (requested,))
    config = _loaded_catalog_config(backend, requested, source)
    route = {"hops": [] if state == "empty" else [{"source_id": source.id, "model_id": "mapped-target"}]}
    if state != "automatic":
        config.agents[backend].routes[requested] = ModelHubRouteConfig.from_payload(route)
    config = ModelHubConfig.from_payload(config.to_payload())
    service, store, adapter = _service(tmp_path, config)
    before = config.to_payload()
    if operation == "put":
        result = asyncio.run(service.set_agent_chain(backend, requested, route))["chain"]
        assert result["manual_override"] == route
    elif operation == "preview":
        result = service.preview_agent_chain(backend, requested, {"manual_override": route})
        assert result["manual_override"] == route
    elif operation == "restore_preview":
        result = service.preview_agent_chain(backend, requested, {"manual_override": None})
        assert result["route_origin"] == "automatic"
        assert result["current"] == {"source_id": source.id, "model_id": requested}
    else:
        if state == "manual":
            with pytest.raises(ModelHubError) as refusal:
                asyncio.run(service.delete_agent_chain(backend, requested))
            assert refusal.value.code == "source_in_route_chain"
            assert refusal.value.data["would_remove_hops"] == [
                {
                    "backend": backend,
                    "menu_model": requested,
                    "source_id": source.id,
                    "model_id": "mapped-target",
                    "position": 1,
                }
            ]
            assert store.config.to_payload() == before
            result = asyncio.run(
                service.delete_agent_chain(
                    backend,
                    requested,
                    {"force": True, **refusal.value.data},
                )
            )["chain"]
        else:
            result = asyncio.run(service.delete_agent_chain(backend, requested))["chain"]
        assert result["manual_override"] is None
        assert result["route_origin"] == "automatic"
        assert requested not in store.config.agents[backend].routes
    assert result["model_id"] == requested
    assert [model.id for model in store.config.agents[backend].models] == [requested]
    assert ModelHubConfig.from_payload(store.config.to_payload()).to_payload() == store.config.to_payload()
    if "preview" in operation:
        assert store.config.to_payload() == before
        assert adapter.synced == []


def test_opencode_existing_loader_still_rejects_overlength_catalog_identity():
    with pytest.raises(ValueError, match="models.id"):
        _loaded_catalog_config("opencode", "x" * (MODEL_ID_MAX_LENGTH + 1))


@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
@pytest.mark.parametrize("kind", ["api_key", "subscription"])
@pytest.mark.parametrize("evidence", ["override", "inventory"])
def test_existing_long_target_identity_can_be_saved_and_reordered(tmp_path, backend, kind, evidence):
    target = "legacy-target-" + "x" * MODEL_ID_MAX_LENGTH
    source = _source("src_legacy001", (target,) if evidence == "inventory" else (), kind=kind)
    other = _source("src_legacy002", ("other",), kind=kind)
    config = _loaded_catalog_config(backend, MODEL, source, other)
    hops = [{"source_id": source.id, "model_id": target}, {"source_id": other.id, "model_id": "other"}]
    if evidence == "override":
        config.agents[backend].routes[MODEL] = ModelHubRouteConfig.from_payload({"hops": hops})
    config = ModelHubConfig.from_payload(config.to_payload())
    service, store, _adapter = _service(tmp_path, config)
    original_inventory = [source.to_payload()["models"] for source in config.sources]
    for desired in (hops, list(reversed(hops))):
        preview = service.preview_agent_chain(backend, MODEL, {"manual_override": {"hops": desired}})
        assert preview["manual_override"] == {"hops": desired}
        saved = asyncio.run(service.set_agent_chain(backend, MODEL, {"hops": desired}))["chain"]
        assert saved["manual_override"] == {"hops": desired}
        loaded = ModelHubConfig.from_payload(store.config.to_payload())
        assert loaded.agents[backend].routes[MODEL].to_payload() == {"hops": desired}
        assert [source.to_payload()["models"] for source in loaded.sources] == original_inventory


@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
@pytest.mark.parametrize("target_kind", ["new_long", "padded_known", "padded_long", "other_source_long"])
def test_new_target_admission_cannot_reuse_legacy_identity_exceptions(tmp_path, backend, target_kind):
    legacy = "legacy-" + "x" * MODEL_ID_MAX_LENGTH
    source = _source("src_legacy001", ("known", legacy))
    other = _source("src_legacy002", ())
    config = _loaded_catalog_config(backend, MODEL, source, other)
    config.agents[backend].routes[MODEL] = ModelHubRouteConfig.from_payload(
        {
            "hops": [{"source_id": source.id, "model_id": legacy}],
        }
    )
    service, store, adapter = _service(tmp_path, ModelHubConfig.from_payload(config.to_payload()))
    target = {
        "new_long": "new-" + "x" * MODEL_ID_MAX_LENGTH,
        "padded_known": " known ",
        "padded_long": f" {legacy} ",
        "other_source_long": legacy,
    }[target_kind]
    route = {"hops": [{"source_id": other.id if target_kind == "other_source_long" else source.id, "model_id": target}]}
    before = store.config.to_payload()
    with pytest.raises(ModelHubError):
        service.preview_agent_chain(backend, MODEL, {"manual_override": route})
    with pytest.raises(ModelHubError):
        asyncio.run(service.set_agent_chain(backend, MODEL, route))
    assert store.config.to_payload() == before
    assert adapter.synced == []


@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
@pytest.mark.parametrize(
    "kind,existing,allowed",
    [
        ("api_key", False, False),
        ("api_key", True, False),
        ("subscription", False, False),
        ("subscription", True, True),
    ],
)
def test_legacy_target_retirement_keeps_existing_source_policy(tmp_path, backend, kind, existing, allowed):
    target = "retired-" + "x" * MODEL_ID_MAX_LENGTH
    source = _source("src_legacy001", (target,), kind=kind)
    source.models[0].retired = True
    config = _loaded_catalog_config(backend, MODEL, source)
    route = {"hops": [{"source_id": source.id, "model_id": target}]}
    if existing:
        config.agents[backend].routes[MODEL] = ModelHubRouteConfig.from_payload(route)
    service, store, adapter = _service(tmp_path, ModelHubConfig.from_payload(config.to_payload()))
    before = store.config.to_payload()
    if allowed:
        preview = service.preview_agent_chain(backend, MODEL, {"manual_override": route})
        assert preview["chain"][0]["runnable"] is False
        assert asyncio.run(service.set_agent_chain(backend, MODEL, route))["chain"] == preview
    else:
        with pytest.raises(ModelHubError):
            service.preview_agent_chain(backend, MODEL, {"manual_override": route})
        with pytest.raises(ModelHubError):
            asyncio.run(service.set_agent_chain(backend, MODEL, route))
        assert store.config.to_payload() == before
        assert adapter.synced == []


@pytest.mark.parametrize(
    "state", ["unknown", "known", "retired", "empty", "subscription_known", "subscription_unknown"]
)
def test_retained_opencode_route_registration_matches_resolver_without_inventing_catalog(tmp_path, state):
    target, dormant, catalog = "dormant-target", "dormant-model", "catalog-model"
    inventory = (catalog, target) if state in {"known", "retired", "subscription_known"} else (catalog,)
    engine_store = EngineStateStore(tmp_path / "engine")
    credential = engine_store.store_api_key(
        "fixture-key", vendor="custom", protocol="openai_responses", base_url="https://api.example/v1"
    )
    source = _source("src_dormant01", inventory, vendor="custom", credential_ref=credential)
    source.base_url = "https://api.example/v1"
    if state.startswith("subscription"):
        source.kind, source.billing = "subscription", "monthly"
        source.vendor, source.base_url = "openai", None
    if state == "retired":
        source.models[-1].retired = True
    config = _loaded_catalog_config("opencode", catalog, source)
    config.agents["opencode"].routes[dormant] = ModelHubRouteConfig.from_payload(
        {
            "hops": [] if state == "empty" else [{"source_id": source.id, "model_id": target}],
        }
    )
    path = tmp_path / "persisted-model-hub.json"
    path.write_text(json.dumps(config.to_payload()))
    config = ModelHubConfig.from_payload(json.loads(path.read_text()))
    service, store, adapter = _service(tmp_path, config)
    before = config.to_payload()
    agent_before = before["agents"]["opencode"]
    plan = effective_model_route(config, "opencode", dormant)
    assert plan.route_origin == (None if state == "empty" else "manual")
    expected_targets = (
        ()
        if state.startswith("subscription")
        else tuple(sorted((catalog,) if state in {"retired", "empty"} else (catalog, target)))
    )
    binding = service._bindings(config)[0]
    assert binding.route_model_ids == expected_targets
    assert binding.model_ids == tuple(model.id for model in config.sources[0].models if not model.retired)
    assert {item["model_id"] for item in service.agent_chains("opencode")} == {catalog, dormant}
    if state != "empty":
        assert {"backend": "opencode", "menu_model": dormant} in service._adopted_by(source.id)
        assert any(item["menu_model"] == dormant for item in service._added_to(source.id))
        with pytest.raises(ModelHubError) as refusal:
            asyncio.run(service.delete_source(source.id))
        assert any(item["menu_model"] == dormant for item in refusal.value.data["would_remove_hops"])
        assert store.config.to_payload() == before
    if not state.startswith("subscription"):
        engine_store.sync_sources([binding])
        compiled = {}
        _append_source(compiled, engine_store.get_source(source.id), engine_store)
        compiled_models = compiled["codex-api-key"][0]["models"]
        assert {item["name"] for item in compiled_models} == set(binding.model_ids) | set(expected_targets)
        assert all(set(item) == {"name", "alias"} for item in compiled_models)
    if state == "unknown":
        adapter.outcomes.append(
            RawCallOutcome(
                kind=RawOutcomeKind.SUCCESS,
                http_status=200,
                error_code=None,
                redacted_message=None,
                stream_started=False,
                source_id=source.id,
                model_id=target,
            )
        )
        probe = asyncio.run(service.probe_agent("opencode", dormant))
        assert probe["reachable"] is True
        assert adapter.invocations == [(source.id, target)]
        assert adapter.synced[-1][0].route_model_ids == expected_targets
    for mode in ("direct", "hub"):
        for health in ("standby", "error"):
            changed = ModelHubConfig.from_payload(before)
            changed.agents["opencode"].mode = mode
            changed.sources[0].state = ModelHubSourceStateConfig(status=health)
            assert service._bindings(changed)[0].route_model_ids == expected_targets
    loaded = ModelHubConfig.from_payload(store.config.to_payload())
    assert loaded.agents["opencode"].to_payload() == agent_before
    assert loaded.sources[0].to_payload()["models"] == before["sources"][0]["models"]
    assert dormant not in {model.id for model in loaded.agents["opencode"].models}
    assert loaded.agents["opencode"].menu.checked == [catalog]
