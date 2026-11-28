"""The same current Source observation offers family models without writing a picker."""
import asyncio
import threading
from copy import deepcopy

import pytest

from core.handlers.model_hub.errors import ModelDiscoveryError
from core.handlers.model_hub.rpc import dispatch_model_hub_rpc
from core.handlers.model_hub.service import ModelHubError
from vibe import model_hub_client

from tests.test_model_hub_resolution import _service
from tests.test_model_hub_background_discovery import ListingAdapter, _background, _hub_source


def test_hub_offer_reads_follow_supply_without_equipping_vibey_or_the_native_agent(tmp_path, monkeypatch):
    mid = "gpt-family-test"
    src = _hub_source("src_familyread", vendor="openai")
    # Higher priority for routing, but its raw inventory is not a current listing.
    stale = _hub_source("src_oldroute", model_ids=(mid,), vendor="openai")
    service, store, adapter, _ = _background(tmp_path, [stale, src], ListingAdapter((mid,)))
    config = deepcopy(store.load())
    config.agents["vibey"].sources.order = [stale.id, src.id]
    store.save(config)
    monkeypatch.setattr("vibe.models_dev_catalog.cached_models_dev_catalog", lambda: {
        "openai": {"name": "OpenAI", "models": {mid: {
            "name": "Family test", "tool_call": True, "modalities": {"input": ["text"], "output": ["text"]},
        }}},
    })

    def menu(backend):
        return {row["id"] for row in asyncio.run(service.read_agent_sources(backend))["catalog_models"]}

    asyncio.run(service._refresh_source_in_background(src.id))
    saved = store.load().to_payload()
    assert mid not in {row["id"] for row in saved["agents"]["codex"]["models"]}
    assert mid in menu("codex")
    from modules.agents.model_hub import ModelHubRuntimeRouter

    router = ModelHubRuntimeRouter(service=service, overlay_path=tmp_path / "overlay.json")
    assert mid in {row.id for row in router.snapshot().agents["codex"].models}
    assert mid in {row["id"] for row in service.backend_catalog_models("codex") if row["routeable"]}
    admitted = service._invocation_resolution(asyncio.run(service._invocation_config("codex", None)), "codex", mid, "hub")
    assert admitted.source is not None and admitted.source.id == src.id
    assert mid not in menu("vibey")
    assert [row["id"] for row in asyncio.run(service.read_hub_offer_candidates("vibey"))] == [mid]
    provider = next(row for row in service.agent_model_candidates("vibey")["providers"] if row["id"] == mid)
    expected = [{"source_id": src.id, "model_id": mid}]
    assert [{key: item[key] for key in ("source_id", "model_id")} for item in provider["suppliers"]] == expected
    assert service._project_expected_suppliers(store.load(), "vibey", [mid]) == {mid: expected}
    assert store.load().to_payload() == saved
    restarted, _, restarted_adapter = _service(tmp_path, deepcopy(store.load()), ListingAdapter(()))
    assert [row["id"] for row in asyncio.run(restarted.read_hub_offer_candidates("vibey"))] == [mid]
    assert not restarted_adapter.listed
    # A preview and an optimistic guard alone don't prove what the next turn uses.
    native = next(row for row in restarted.agent_model_candidates("codex")["in_list"] if row["id"] == mid)
    assert [s["source_id"] for s in native["suppliers"]] == [src.id]
    vibey_baseline = [row.to_payload() for row in restarted.store.load().agents["vibey"].models]
    offered, = restarted._hub_offer_rows(restarted.store.load(), "vibey")
    asyncio.run(restarted.set_agent_models("vibey", vibey_baseline, [*vibey_baseline, offered.to_payload()],
                                          expected_suppliers={mid: expected}))
    persisted = restarted.store.load()
    assert [hop.to_payload() for hop in persisted.agents["vibey"].routes[mid].hops] == expected
    route = restarted._invocation_resolution(persisted, "vibey", mid, "hub")
    assert [hop.source.id for hop in route.candidate_hops] == [src.id]
    assert adapter.listed == [src.credential_ref]
    baseline = [row.to_payload() for row in router.snapshot().agents["codex"].models]
    reordered = [next(row for row in baseline if row["id"] == mid), *[row for row in baseline if row["id"] != mid]]
    changed = asyncio.run(service.set_agent_models("codex", baseline, reordered))
    assert changed["agent"]["catalog_models"][0]["id"] == mid
    assert all(row.id != mid for row in store.load().agents["codex"].models)
    after_reorder, _, _ = _service(tmp_path, deepcopy(store.load()), ListingAdapter(()))
    assert after_reorder.backend_catalog_models("codex")[0]["id"] == mid
    result = asyncio.run(service.set_agent_models("codex", reordered, [row for row in reordered if row["id"] != mid]))
    assert mid not in {row["id"] for row in result["agent"]["catalog_models"]}
    assert mid in store.load().agents["codex"].removed_model_ids
    assert all(row.id != mid for row in store.load().agents["codex"].models)
    assert mid not in menu("codex")
    assert mid not in {row.id for row in router.snapshot().agents["codex"].models}
    assert mid not in {row["id"] for row in service.backend_catalog_models("codex")}
    # A retained route is not a listing under the replacement credential.
    config = deepcopy(store.load())
    next(source for source in config.sources if source.id == src.id).credential_generation = "vp_" + "a" * 32
    store.save(config)
    assert asyncio.run(service.read_hub_offer_candidates("vibey")) == []


@pytest.mark.parametrize("served", [True, False], ids=["assigned-route", "no-assigned-route"])
def test_direct_pin_binding_is_decided_at_switch_not_at_launch(tmp_path, monkeypatch, served):
    from core.handlers.model_hub.saved_pick import PICK_METADATA_KEY, SavedHubPick
    from core.vibe_agents import VibeAgentStore
    from modules.agents.model_hub import ModelHubRuntimeRouter

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    mid = "relay-private-test"
    source = _hub_source("src_switchroute", model_ids=(mid,), vendor="openai")
    service, store, _, _ = _background(tmp_path, [source])
    config = deepcopy(store.load())
    native = config.agents["codex"]
    native.mode = "direct"
    native.sources.order = [source.id] if served else []
    store.save(config)
    agent_db = tmp_path / "agents.db"
    agents = VibeAgentStore(agent_db)
    try:
        selected = agents.create(name="legacy-relay", backend="codex", model=mid)
        assert PICK_METADATA_KEY not in selected.metadata
        service.native_agent_picks = agents
        service.named_agents_override = lambda _: [(selected.name, mid)]
        async def unblocked(*_args):
            return False
        monkeypatch.setattr(service, "_native_config_blocks_launch", unblocked)
        asyncio.run(service.set_agent_mode("codex", "hub"))
    finally:
        agents.close()
    reader = VibeAgentStore(agent_db, read_only=True)
    try:
        agent = reader.get(selected.name)
        assert agent.model == mid
        pick = SavedHubPick.from_payload(agent.metadata[PICK_METADATA_KEY])
        assert pick and [h.hop.source_id for h in pick.hops] == ([source.id] if served else [])
    finally:
        reader.close()
    after = deepcopy(store.load())
    assert all(row.id != mid for row in after.agents["codex"].models)
    # After restart, an assignment added too late cannot create consent; a
    # credential replacing the captured one cannot inherit it either.
    after.agents["codex"].sources.order = [source.id]
    if served:
        after.sources[0].credential_generation = "vp_" + "0" * 32
    restarted, _, _ = _service(tmp_path, after, ListingAdapter(()))
    router = ModelHubRuntimeRouter(service=restarted)
    with pytest.raises(ModelHubError) as refused:
        asyncio.run(router.resolve("codex", mid, saved_pick=pick))
    assert refused.value.code == "saved_source_unavailable"


def test_offered_native_route_can_be_edited_without_adding_a_menu_row(tmp_path, monkeypatch):
    from config.v2_config import ModelHubConfig
    from core.handlers.model_hub.saved_pick import SavedHubPick
    from core.handlers.model_hub.service import capture_explicit_native_pick

    mid = "gpt-route-edit-test"
    source = _hub_source("src_editroute", vendor="openai")
    service, store, _, _ = _background(tmp_path, [source], ListingAdapter((mid,)))
    monkeypatch.setattr("vibe.models_dev_catalog.cached_models_dev_catalog", lambda: {
        "openai": {"name": "OpenAI", "models": {mid: {"name": mid, "tool_call": True,
            "modalities": {"input": ["text"], "output": ["text"]}}}},
    })
    asyncio.run(service._refresh_source_in_background(source.id))
    before = deepcopy(store.load().agents["codex"].models)
    monkeypatch.setattr("config.paths.get_state_dir", lambda: tmp_path)
    monkeypatch.setattr("core.handlers.model_hub.service.V2ModelHubConfigStore.load", lambda _: store.load())
    # A distinct target proves both preview and the eventual selection use the
    # override, rather than coincidentally returning the listing's auto-route.
    patch = {"hops": [{"source_id": source.id, "model_id": "relay-target"}]}
    assert service.agent_chain("codex", mid)["route_origin"] == "automatic"
    preview = service.preview_agent_chain("codex", mid, {"manual_override": patch})
    assert preview["route_origin"] == "manual"
    assert store.load().agents["codex"].models == before
    response = asyncio.run(service.set_agent_chain("codex", mid, patch))
    assert response["chain"]["route_origin"] == "manual"
    restored = ModelHubConfig.from_payload(store.load().to_payload())
    assert restored.agents["codex"].models == before
    assert mid not in restored.agents["codex"].routes  # Existing strict/downgrade format stays unchanged.
    restarted, _, _ = _service(tmp_path, restored, ListingAdapter(()))
    chain = restarted.agent_chain("codex", mid)
    assert chain["manual_override"]["hops"] == patch["hops"]
    assert chain in restarted.agent_chains("codex")
    captured = capture_explicit_native_pick("codex", mid)
    assert captured is not None
    selected = SavedHubPick.from_payload(captured.to_payload())
    assert selected and selected.manual and [entry.hop.to_payload() for entry in selected.hops] == patch["hops"]
    view = selected.execution_view(restored, "codex", mid)
    resolution = restarted._invocation_resolution(view, "codex", mid, "hub")
    assert (resolution.source.id, resolution.target_model) == (source.id, "relay-target")

    # Clearing this override requires confirming the exact hop; a rejected
    # clear must not change the pick seen by another process.
    with pytest.raises(ModelHubError) as refused:
        asyncio.run(restarted.delete_agent_chain("codex", mid))
    assert refused.value.code == "source_in_route_chain"
    plan = {key: refused.value.data[key] for key in ("would_remove_hops", "would_interrupt")}
    assert [hop["model_id"] for hop in plan["would_remove_hops"]] == ["relay-target"]
    assert capture_explicit_native_pick("codex", mid).manual
    asyncio.run(restarted.delete_agent_chain("codex", mid, {"force": True, **plan}))
    auto = capture_explicit_native_pick("codex", mid)
    assert auto and not auto.manual and [entry.hop.model_id for entry in auto.hops] == [mid]

    # On a damaged preference file, neither a new picker nor a stale picker
    # selection may silently send the request through the automatic supplier.
    path = restarted._offer_routes["codex"].path
    path.write_bytes(b"{truncated")
    damaged, _, _ = _service(tmp_path, restored, ListingAdapter(()))
    assert mid not in {row["id"] for row in damaged.backend_catalog_models("codex")}
    refused_pick = capture_explicit_native_pick("codex", mid)
    assert refused_pick and not refused_pick.hops
    assert path.read_bytes() == b"{truncated"


@pytest.mark.parametrize("proof", ["verified-setup", "gated-list", "public-list"])
def test_api_key_setup_publishes_only_authenticated_listing_after_commit(tmp_path, monkeypatch, proof):
    from config.v2_config import ModelHubModelConfig
    from core.handlers.model_hub.adapter import ApiKeyModelsCheck, DiscoveredModel

    mid, manual = "gpt-key-list-test", "gpt-key-manual-test"
    service, store, adapter, _ = _background(tmp_path, [], ListingAdapter((mid,)))
    monkeypatch.setattr("vibe.models_dev_catalog.cached_models_dev_catalog", lambda: {
        "openai": {"name": "OpenAI", "models": {m: {"name": m, "tool_call": True,
            "modalities": {"input": ["text"], "output": ["text"]}} for m in (mid, manual)}},
    })
    async def key_check(*_args, **_kwargs):
        return ApiKeyModelsCheck(authenticated=True if proof == "gated-list" else None,
                                 models=(DiscoveredModel(id=mid),))
    monkeypatch.setattr(adapter, "check_api_key_models", key_check)
    original_save = service._listing_file.save
    publications = []
    def publish(rows):
        # Inspect the real order of persistence, not just the eventual state.
        assert set(rows) == {source.id for source in store.load().sources}
        publications.append(tuple(receipt.model_ids for receipt in rows.values()))
        original_save(rows)
    monkeypatch.setattr(service._listing_file, "save", publish)
    result = asyncio.run(service.create_source({"kind": "api_key", "vendor": "openai", "protocol": "openai_responses",
        "key": "fixture-only", "models": [ModelHubModelConfig(id=manual, provenance="manual").to_payload()],
        "save_unverified": proof != "verified-setup"}))
    source = store.load().sources[0]
    assert source.id == result["source"]["id"] and {row.id for row in source.models} == {mid, manual}
    restarted, _, _ = _service(tmp_path, deepcopy(store.load()), ListingAdapter(()))
    offers = {row.id for row in restarted._hub_offer_rows(restarted.store.load(), "codex")}
    if proof == "public-list":
        assert source.verification_pending and not publications and not offers
    else:
        assert not source.verification_pending and publications == [((mid,),)] and offers == {mid}


def test_fork_inherits_withdrawn_pick_but_an_explicit_override_captures_its_own(tmp_path, monkeypatch):
    from config import paths
    from core.handlers.model_hub.saved_pick import read_session_pick
    from core.services.session_fork import reserve_forked_session
    from tests.test_session_fork import _seed_source_session

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    db = paths.get_sqlite_state_path()
    original = _seed_source_session(db, tmp_path)
    x, y = "gpt-fork-parent", "gpt-fork-override"
    a, b = (_hub_source(sid, vendor="openai") for sid in ("src_parentpin", "src_childpin"))
    service, store, adapter, _ = _background(paths.get_state_dir(), [a, b], ListingAdapter((x,)))
    monkeypatch.setattr("core.handlers.model_hub.service.V2ModelHubConfigStore.load", lambda _: store.load())
    monkeypatch.setattr("vibe.models_dev_catalog.cached_models_dev_catalog", lambda: {
        "openai": {"name": "OpenAI", "models": {m: {"name": m, "tool_call": True,
            "modalities": {"input": ["text"], "output": ["text"]}} for m in (x, y)}},
    })
    asyncio.run(service._refresh_source_in_background(a.id))
    from storage.db import get_cached_sqlite_engine
    from storage.workbench_sessions_service import update_session
    from vibe.authorization import AuthorizationContext

    parent = original  # A real native thread, so both forks have a valid fork point.
    with get_cached_sqlite_engine().begin() as conn:
        update_session(conn, parent, model=x, authorization_context=AuthorizationContext(instance_role="owner"))
    # Complete withdrawal at A followed by a distinct offer at B. An inherited
    # fork must not rediscover X; an explicit --model Y must not copy X's pin.
    adapter.discovered = ()
    asyncio.run(service._refresh_source_in_background(a.id))
    adapter.discovered = (y,)
    asyncio.run(service._refresh_source_in_background(b.id))
    inherited = reserve_forked_session(source_session_id=parent, db_path=db).session_id
    changed = reserve_forked_session(source_session_id=parent, model=y, db_path=db).session_id
    for session, mid, sid in ((parent, x, a.id), (inherited, x, a.id), (changed, y, b.id)):
        pick = read_session_pick(session, "codex", mid)
        assert pick and [(entry.hop.source_id, entry.hop.model_id) for entry in pick.hops] == [(sid, mid)]


@pytest.mark.parametrize("kind", ["api_key", "subscription"])
def test_guarded_refresh_keeps_old_routes_but_new_listing_is_runnable(tmp_path, monkeypatch, kind):
    from core.handlers.model_hub.adapter import DiscoveredModel

    old, new = "claude-opus-4-6", "claude-guarded-offer"
    source = _hub_source("src_guardread", model_ids=(old,), kind=kind)
    service, store, adapter, _ = _background(tmp_path, [source])
    monkeypatch.setattr("vibe.models_dev_catalog.cached_models_dev_catalog", lambda: {
        "anthropic": {"name": "Anthropic", "models": {new: {"name": new, "tool_call": True,
            "modalities": {"input": ["text"], "output": ["text"]}, "limit": {"context": 123456}}}},
    })

    async def list_new(*_args, **_kwargs):
        return (DiscoveredModel(id=new, supported_parameters=("reasoning",)),)

    monkeypatch.setattr(adapter, "discover_models", list_new)
    before = store.load().to_payload()
    with pytest.raises(ModelHubError) as refusal:
        asyncio.run(service.refresh_source(source.id))
    assert refusal.value.data["would_remove_hops"]
    assert store.load().to_payload() == before
    assert [m.id for m in store.load().sources[0].models] == [old]
    restarted, _, _ = _service(tmp_path, deepcopy(store.load()), ListingAdapter(()))
    receipt = restarted._complete_hub_listings[source.id]
    assert receipt.model_ids == receipt.reasoning_ids == (new,)
    view = asyncio.run(restarted._invocation_config("claude", None))
    row = next(row for row in view.agents["claude"].models if row.id == new)
    assert row.context_window == 123456 and row.reasoning_efforts
    resolution = restarted._invocation_resolution(view, "claude", new, "hub")
    assert resolution.source and (resolution.source.id, resolution.target_model) == (source.id, new)


def test_manual_observation_beats_a_late_background_read_and_survives_a_failed_read(tmp_path, monkeypatch):
    sid = "src_readrace"
    first, second = "gpt-first-test", "gpt-second-test"
    src = _hub_source(sid, model_ids=(), vendor="openai")
    service, store, adapter, _ = _background(tmp_path, [src], ListingAdapter((second,)))
    config = deepcopy(store.load())
    config.agents["vibey"].sources.order = [sid]
    store.save(config)
    monkeypatch.setattr("vibe.models_dev_catalog.cached_models_dev_catalog", lambda: {
        "openai": {"name": "OpenAI", "models": {mid: {"name": mid, "tool_call": True,
            "modalities": {"input": ["text"], "output": ["text"]}} for mid in (first, second)}},
    })

    async def exercise():
        started, finish = asyncio.Event(), asyncio.Event()
        discover = adapter.discover_models

        async def delayed(*args, start_engine=True):
            if not start_engine:
                started.set()
                await finish.wait()
                adapter.discovered = (first,)
            return await discover(*args, start_engine=start_engine)

        monkeypatch.setattr(adapter, "discover_models", delayed)
        background = asyncio.create_task(service._refresh_source_in_background(sid))
        await asyncio.wait_for(started.wait(), 2)
        try:
            await service.refresh_source(sid)
        finally:
            finish.set()
            await background
        adapter.discovery_error = ModelDiscoveryError("unavailable")
        with pytest.raises(ModelHubError, match="discovery_failed"):
            await service.refresh_source(sid)

    asyncio.run(exercise())
    restarted, _, new_adapter = _service(tmp_path, deepcopy(store.load()), ListingAdapter(()))

    async def rpc(operation, payload):
        return await dispatch_model_hub_rpc(restarted, operation, payload)

    monkeypatch.setattr(model_hub_client, "_rpc", rpc)
    rows = asyncio.run(model_hub_client.ModelHubRemoteService().read_hub_offer_candidates("vibey"))
    assert [row["id"] for row in rows] == [second]
    assert not new_adapter.listed


@pytest.mark.parametrize("surface", ["channel", "thread", "user"])
@pytest.mark.parametrize("inherit", [False, True], ids=["scope-choice", "agent-choice"])
def test_full_settings_save_keeps_withdrawn_claude_off(tmp_path, monkeypatch, surface, inherit):
    from config import paths
    from config.v2_settings import SettingsStore
    from core.vibe_agents import VibeAgentStore
    from storage.importer import ensure_sqlite_state
    from vibe import api
    from vibe.authorization import AuthorizationContext

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    ensure_sqlite_state()
    mid = "claude-private-effort"
    source = _hub_source("src_effortpin", model_ids=(mid,))
    source.models[0].reasoning_efforts = ["off", "high"]
    source.models[0].reasoning_efforts_source = "user"
    service, store, _, _ = _background(paths.get_state_dir(), [source], ListingAdapter((mid,)))
    monkeypatch.setattr("core.handlers.model_hub.service.V2ModelHubConfigStore.load", lambda _: store.load())
    monkeypatch.setattr("vibe.models_dev_catalog.cached_models_dev_catalog", lambda: {
        "anthropic": {"name": "Anthropic", "models": {mid: {"name": mid, "tool_call": True,
            "modalities": {"input": ["text"], "output": ["text"]}}}},
    })
    asyncio.run(service._refresh_source_in_background(source.id))
    agents = VibeAgentStore()
    owner = AuthorizationContext(instance_role="owner")
    SettingsStore.reset_instance()
    try:
        # The explicit-scope case deliberately gives the Agent another model:
        # it cannot pass by consulting the Agent's own ladder.
        agent = agents.create(name="effort-scope", backend="claude", model=mid if inherit else "claude-other")
        routing = {"agent_name": agent.name, "model": None if inherit else mid, "reasoning_effort": "off"}

        def save(cwd):
            body = {"routing": routing, "custom_cwd": str(cwd)}
            if surface == "user":
                api.save_users({"platform": "telegram", "users": {"42": body}}, user_context=owner)
            elif surface == "channel":
                api.save_settings({"platform": "telegram", "channels": {"-1001": body}}, user_context=owner)
            else:
                result = api.save_thread_settings({"platform": "telegram", "channel_id": "-1001", "thread_id": "42",
                                                   "settings": body}, user_context=owner)
                assert result["ok"]
            SettingsStore.reset_instance()
            with_store = SettingsStore()
            try:
                if surface == "user":
                    item = with_store.get_user("42", platform="telegram")
                elif surface == "channel":
                    item = with_store.find_channel("-1001", platform="telegram")
                else:
                    item = with_store.find_thread("-1001", "42", platform="telegram")
                assert item.custom_cwd == str(cwd)
                assert (item.routing.model, item.routing.reasoning_effort) == (routing["model"], "off")
            finally:
                with_store.close()

        save(tmp_path)
        service._listing_file.path.unlink()
        assert mid not in {m["id"] for m in service.backend_catalog_models("claude")}
        save(tmp_path.parent)
    finally:
        agents.close()
        SettingsStore.reset_instance()


def test_same_model_reselection_rebinds_only_on_explicit_scope_change(tmp_path, monkeypatch):
    from config import paths
    from config.v2_settings import SettingsStore
    from core.handlers.model_hub.saved_pick import SavedHubPick
    from core.services.scope_settings import apply_scope_change
    from core.vibe_agents import VibeAgentStore
    from storage.importer import ensure_sqlite_state
    from vibe.authorization import AuthorizationContext

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    ensure_sqlite_state()
    mid = "gpt-reselection-test"
    a, b = (_hub_source(sid, vendor="openai") for sid in ("src_scopeold", "src_scopenew"))
    service, store, adapter, _ = _background(paths.get_state_dir(), [a, b], ListingAdapter((mid,)))
    monkeypatch.setattr("core.handlers.model_hub.service.V2ModelHubConfigStore.load", lambda _: store.load())
    monkeypatch.setattr("vibe.models_dev_catalog.cached_models_dev_catalog", lambda: {
        "openai": {"name": "OpenAI", "models": {mid: {"name": mid, "tool_call": True,
            "modalities": {"input": ["text"], "output": ["text"]}}}},
    })
    agents = VibeAgentStore()
    try:
        agent = agents.create(name="scope-reselect", backend="codex")
    finally:
        agents.close()

    def change(patch, expected):
        apply_scope_change(SettingsStore(), platform="slack", kind="channel", native_id="reselect",
            changes={"routing": patch}, expected_agent_name=expected, user_context=AuthorizationContext(instance_role="owner"))
        # Load through a fresh store; don't rely on the mutation's in-memory return value.
        route = next(item.routing for item in SettingsStore().settings.channels.values() if item.routing.agent_name == agent.name)
        pick = SavedHubPick.from_payload(route._hub_pick)
        assert pick and pick.model.id == mid
        return [binding.hop.source_id for binding in pick.hops]

    asyncio.run(service._refresh_source_in_background(a.id))
    agents = VibeAgentStore()
    try:
        agent = agents.update(agent.name, model=mid)
    finally:
        agents.close()
    assert change({"agent_name": agent.name, "model": mid}, None) == [a.id]
    adapter.discovered = ()
    asyncio.run(service._refresh_source_in_background(a.id))
    adapter.discovered = (mid,)
    asyncio.run(service._refresh_source_in_background(b.id))
    assert change({"reasoning_effort": "high"}, agent.name) == [a.id]
    assert change({"model": mid}, agent.name) == [b.id]

    # Both owners chose the same ID but different grants. With the receipt
    # gone, a new IM session must snapshot the winner, not recapture the offer.
    from core.handlers.model_hub.saved_pick import read_session_pick
    from core.services.agent_run_target import resolve_agent_run_target
    from modules.im import MessageContext
    from tests.test_agent_run_target import _controller

    service._listing_file.path.unlink()
    controller = _controller(tmp_path)
    controller.resolve_vibe_agent_for_context = lambda *_args, **_kwargs: agent

    def session_source(thread):
        ctx = MessageContext(user_id="u", channel_id="reselect", platform="slack", thread_id=thread)
        target = resolve_agent_run_target(ctx, controller=controller, base_session_id=f"slack_{thread}")
        pick = read_session_pick(target.agent_session_id, "codex", mid)
        assert pick and target.model == mid
        return [binding.hop.source_id for binding in pick.hops]

    assert session_source("scope-wins") == [b.id]
    apply_scope_change(SettingsStore(), platform="slack", kind="channel", native_id="reselect",
        changes={"routing": {"model": None}}, expected_agent_name=agent.name,
        user_context=AuthorizationContext(instance_role="owner"))
    assert session_source("agent-wins") == [a.id]


@pytest.mark.parametrize("binding", ["unchanged", "unassigned", "replaced", "legacy", "legacy_unassigned",
                                     "unassigned_after_launch", "replaced_after_launch"])
def test_saved_pick_at_gateway_admission_after_receipt_loss(tmp_path, monkeypatch, binding):
    from types import SimpleNamespace
    import json
    import aiohttp
    from core.handlers.model_hub.adapter import RawCallOutcome, RawOutcomeKind
    from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
    from modules.agents.model_hub import ModelHubRuntimeRouter, resolve_model_hub_launch
    from config import paths
    from core.handlers.model_hub.saved_pick import PICK_METADATA_KEY, read_session_pick
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.workbench_sessions_service import create_session, update_session
    from vibe.authorization import AuthorizationContext

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    ensure_sqlite_state()
    db = create_sqlite_engine()
    owner = AuthorizationContext(instance_role="owner")
    mid = "gpt-saved-test"
    src = _hub_source("src_savedpick", vendor="openai")
    service, store, adapter, _ = _background(paths.get_state_dir(), [src], ListingAdapter((mid,)))
    monkeypatch.setattr("core.handlers.model_hub.service.V2ModelHubConfigStore.load", lambda _: store.load())
    monkeypatch.setattr("vibe.models_dev_catalog.cached_models_dev_catalog", lambda: {
        "openai": {"name": "OpenAI", "models": {mid: {"name": mid, "tool_call": True,
            "limit": {"context": 123456, "output": 8192},
            "modalities": {"input": ["text", "image"], "output": ["text"]}}}},
    })
    async def list_with_reasoning(*_args, **_kwargs):
        from core.handlers.model_hub.adapter import DiscoveredModel

        return tuple(DiscoveredModel(id=item, supported_parameters=("reasoning",)) for item in adapter.discovered)

    monkeypatch.setattr(adapter, "discover_models", list_with_reasoning)
    legacy = binding.startswith("legacy")
    if legacy:
        # Released configs could serve a saved built-in selection automatically from its
        # assigned Source inventory. They have neither a descriptor nor a manual chain.
        from config.v2_config import ModelHubBackendModelConfig
        config = deepcopy(store.load())
        config.agents["codex"].models.append(ModelHubBackendModelConfig(id=mid, origin="builtin"))
        assert mid not in config.agents["codex"].routes
        store.save(config)
        monkeypatch.setattr(type(service), "_withdrawn_builtin_model_ids", lambda self, backend: {mid})
    asyncio.run(service._refresh_source_in_background(src.id))
    with db.begin() as conn:
        session = create_session(conn, scope_id=None, agent_backend="codex", model=mid, authorization_context=owner,
                                 metadata={PICK_METADATA_KEY: {"fake": True}})
    sid = session["id"]
    assert PICK_METADATA_KEY not in session["metadata"]
    captured = read_session_pick(sid, "codex", mid)
    if legacy:
        assert captured is None
    else:
        assert captured and captured.hops[0].hop.source_id == src.id
    saved = captured.to_payload() if captured else None
    if binding == "unchanged":
        from core.vibe_agents import VibeAgentStore
        catalog = VibeAgentStore()
        try:
            selected = catalog.create(name="saved-agent", backend="codex", model=mid, metadata={PICK_METADATA_KEY: {"fake": True}})
            assert selected.metadata[PICK_METADATA_KEY] == saved
            edited = catalog.update(selected.name, metadata={PICK_METADATA_KEY: {"fake": True}, "label": "mine"})
            assert edited.metadata[PICK_METADATA_KEY] == saved and edited.metadata["label"] == "mine"
            cleared = catalog.update(selected.name, model=None)
            assert PICK_METADATA_KEY not in cleared.metadata
        finally:
            catalog.close()
        from config.v2_settings import SettingsStore, normalize_routing_settings, routing_to_dict
        from core.services.scope_settings import apply_scope_change

        def change_scope(changes, expected):
            return apply_scope_change(SettingsStore(), platform="slack", kind="channel", native_id="saved-channel",
                changes=changes, expected_agent_name=expected, user_context=owner)

        chosen = change_scope({"routing": {"agent_name": selected.name, "model": mid}}, None)
        assert normalize_routing_settings(chosen.routing)._hub_pick == saved
        assert PICK_METADATA_KEY not in routing_to_dict(chosen.routing)
    adapter.discovered = ()
    asyncio.run(service._refresh_source_in_background(src.id))
    assert mid not in {row["id"] for row in service.backend_catalog_models("codex")}
    service._listing_file.path.unlink()
    if binding == "unchanged":
        edited = change_scope({"routing": {"reasoning_effort": "high"}}, selected.name)
        assert edited.routing.model == mid and edited.routing._hub_pick == saved
        cleared = change_scope({"routing": {"model": None}}, selected.name)
        assert cleared.routing._hub_pick is None
    config = deepcopy(store.load())
    if binding in ("unassigned", "legacy_unassigned"):
        config.agents["codex"].sources.order = []
    elif binding == "replaced":
        config.sources[0].credential_generation = "vp_" + "b" * 32
    restarted, new_store, new_adapter = _service(tmp_path, config, ListingAdapter(()))
    with db.begin() as conn:
        update_session(conn, sid, model=mid, reasoning_effort="high", authorization_context=owner)
    pick = read_session_pick(sid, "codex", mid)
    assert (pick.to_payload() if pick else None) == saved

    async def run_saved_session():
        language = "zh" if binding == "replaced_after_launch" else "en"
        gateway = ModelHubTurnGateway(restarted, language_provider=lambda: language)
        router = ModelHubRuntimeRouter(service=restarted, turn_gateway=gateway, overlay_path=tmp_path / "overlay.json")
        owner_thread = threading.get_ident()
        project = restarted.project_offer_menu
        projection_reads = []

        def off_loop_projection(config, backend):
            assert threading.get_ident() != owner_thread
            projection_reads.append(backend)
            return project(config, backend)

        monkeypatch.setattr(restarted, "project_offer_menu", off_loop_projection)
        context = SimpleNamespace(platform_specific={"agent_session_target": {"id": sid}})
        try:
            launch = await resolve_model_hub_launch(SimpleNamespace(model_hub_runtime=router), "codex", mid,
                context=context, process_scope=sid, turn_id="turn_saved_offer")
            if binding == "unchanged":
                from tests.test_codex_catalog_retention import agent as catalog_agent
                from vibe import backend_model_catalog

                monkeypatch.setattr(backend_model_catalog, "_export_codex_bundled_catalog",
                                    lambda *_: b'{"models":[{"slug":"unrelated"}]}')
                codex = catalog_agent()
                cwd = str(tmp_path / "catalog-cwd")
                assert "codex" in projection_reads
                inputs = codex._launch_inputs(cwd, hub_config=await asyncio.to_thread(router.snapshot))
                spec = await codex._launch_spec(cwd, launch, inputs=inputs)
                try:
                    rows = json.loads(spec.catalog.path.read_bytes())["models"]
                    selected, = [row for row in rows if row["slug"] == mid]
                    assert selected["context_window"] == 123456
                    assert set(selected["input_modalities"]) == {"text", "image"}
                    assert "high" in {tier["effort"] for tier in selected["supported_reasoning_levels"]}
                    assert mid not in {row.id for row in inputs.hub_config.agents["codex"].models}
                finally:
                    spec.catalog.close()
            if binding.endswith("_after_launch"):
                changed = deepcopy(new_store.load())
                if binding == "unassigned_after_launch":
                    changed.agents["codex"].sources.order = []
                else:
                    changed.sources[0].credential_generation = "vp_" + "b" * 32
                new_store.save(changed)
            async with aiohttp.ClientSession(trust_env=False) as client:
                async with client.post(f"{launch.gateway_base_url}/v1/responses",
                        headers={"Authorization": f"Bearer {launch.gateway_token}",
                                 "x-codex-turn-metadata": json.dumps(launch.gateway_request_metadata)},
                        json={"model": mid, "input": "test", "stream": False}) as response:
                    if binding.endswith("_after_launch"):
                        assert response.status == 409
                        error = (await response.json())["error"]
                        assert error["code"] == "saved_source_unavailable"
                        assert new_adapter.invocations == []
                        expected = (
                            f"模型 {mid} 已不再提供：它的供应商不可用或已被替换。请选择其他模型后继续。"
                            if language == "zh" else
                            f"Model {mid} is no longer supplied: its provider is unavailable or has been replaced. "
                            "Choose another model to continue."
                        )
                        assert error["message"] == expected
                    else:
                        assert response.status == 200, await response.text()
        finally:
            await gateway.close()
            monkeypatch.setattr(restarted, "project_offer_menu", project)

    if binding in ("unchanged", "legacy"):
        new_adapter.outcomes.append(RawCallOutcome(kind=RawOutcomeKind.SUCCESS, http_status=200,
            error_code=None, redacted_message=None, stream_started=False, model_id=mid, source_id=src.id))
        asyncio.run(run_saved_session())
        assert new_adapter.invocations == [(src.id, mid)]
    elif binding.endswith("_after_launch"):
        asyncio.run(run_saved_session())
        assert new_adapter.invocations == []
    else:
        with pytest.raises(ModelHubError):
            asyncio.run(run_saved_session())
        assert new_adapter.invocations == []
    assert mid not in {row["id"] for row in restarted.backend_catalog_models("codex")}
    assert any(row.id == mid for row in new_store.load().agents["codex"].models) == legacy


def test_blocked_observation_does_not_delay_mode_switch_or_same_handle_renewal(tmp_path, monkeypatch):
    from dataclasses import replace
    from tests.test_model_hub_api import _service as oauth_service

    service, store, adapter = oauth_service(tmp_path)
    observed = _hub_source("src_diskblock")
    renewed = _hub_source("src_renewal01", kind="subscription")
    renewed.state = replace(renewed.state, status="needs_action", detail_key="models.source.needs_action.oauth_expired")
    store.config.sources = [observed, renewed]
    agent = store.config.agents["claude"]
    agent.mode = "direct"
    agent.sources.order = [observed.id, renewed.id]
    service.named_agents_override = lambda backend: [("private-pin", "claude-opus-4-6")]
    before_models = [row.to_payload() for row in agent.models]
    before_generation = renewed.credential_generation
    started, release = threading.Event(), threading.Event()
    save = service._listing_file.save

    def delayed(receipts):
        started.set()
        assert release.wait(8)
        save(receipts)

    monkeypatch.setattr(service._listing_file, "save", delayed)

    async def exercise():
        pending = asyncio.create_task(service._refresh_source_in_background(observed.id))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            assert observed.id not in service._complete_hub_listings  # not yet visible to either process
            await asyncio.wait_for(service.set_agent_mode("claude", "hub"), 2)
            flow = await asyncio.wait_for(service.reauth_source(renewed.id, {"acknowledge_irreversible": True}), 2)
            fid = flow["flow"]["flow_id"]
            adapter.flows[fid] = replace(adapter.flows[fid], state="success", credential_ref=renewed.credential_ref)
            result = await asyncio.wait_for(service.oauth_status(fid), 2)
            assert result["recovered"] and not pending.done()
            assert next(s for s in store.load().sources if s.id == renewed.id).credential_generation != before_generation
            assert [row.to_payload() for row in store.load().agents["claude"].models] == before_models
        finally:
            release.set()
            await pending
        assert observed.id in service._complete_hub_listings

    asyncio.run(exercise())


@pytest.mark.parametrize("listing", ["fails", "hangs"])
def test_renewal_commits_generation_without_discovery_and_rejects_old_listing(tmp_path, monkeypatch, listing):
    from dataclasses import replace
    from core.handlers.model_hub.adapter import DiscoveredModel
    from tests.test_model_hub_api import _service as oauth_service

    service, store, adapter = oauth_service(tmp_path)
    source = _hub_source("src_grantfence", kind="subscription")
    store.config.sources = [source]
    store.config.agents["claude"].sources.order = [source.id]
    calls = []

    async def exercise():
        started, resume = asyncio.Event(), asyncio.Event()

        async def discover(*args, start_engine=True):
            calls.append(start_engine)
            if start_engine:
                if listing == "fails":
                    raise ModelDiscoveryError("unavailable")
                await asyncio.Event().wait()
            started.set()
            await resume.wait()
            return (DiscoveredModel(id="claude-old-account"),)

        monkeypatch.setattr(adapter, "discover_models", discover)
        old_listing = asyncio.create_task(service._refresh_source_in_background(source.id))
        try:
            await asyncio.wait_for(started.wait(), 1)
            flow = (await service.reauth_source(source.id, {"acknowledge_irreversible": True}))["flow"]
            fid = flow["flow_id"]
            adapter.flows[fid] = replace(adapter.flows[fid], state="success", credential_ref=source.credential_ref)
            await asyncio.wait_for(service.oauth_status(fid), 1)
            current = store.load().sources[0]
            assert current.credential_ref == source.credential_ref
            assert current.credential_generation and current.credential_generation != source.credential_generation
            assert current.verification_pending and calls == [False]
            assert not service._complete_hub_listings
        finally:
            resume.set()
            await old_listing
        assert not service._complete_hub_listings
        assert "claude-old-account" not in {row.id for row in store.load().sources[0].models}

        async def new_grant_listing(*args, **kwargs):
            return (DiscoveredModel(id="claude-new-account"),)

        monkeypatch.setattr(adapter, "discover_models", new_grant_listing)
        await service._refresh_source_in_background(source.id)
        receipt = service._complete_hub_listings[source.id]
        assert receipt.model_ids == ("claude-new-account",)
        assert store.load().sources[0].verification_pending  # inventory is not verification of inference

    asyncio.run(exercise())


@pytest.mark.parametrize("operation", ["get_agent_sources", "get_agent_chain", "get_agent_chains", "preview_agent_chain"])
def test_offer_projection_disk_wait_leaves_controller_loop_free(tmp_path, monkeypatch, operation):
    service, _, _, _ = _background(tmp_path, [_hub_source("src_readloop")])
    blocked, release = threading.Event(), threading.Event()
    disk_threads, recovery_threads = [], []
    read = service._listing_file.load
    annotations = service.recovery.annotations

    def slow_disk():
        disk_threads.append(threading.get_ident())
        blocked.set()
        assert release.wait(3), "receipt read blocked the controller loop"
        return read()

    def live(config):
        recovery_threads.append(threading.get_ident())
        return annotations(config)

    monkeypatch.setattr(service._listing_file, "load", slow_disk)
    monkeypatch.setattr(service.recovery, "annotations", live)

    async def exercise():
        owner = threading.get_ident()
        payload = {"backend": "claude", "model_id": "claude-opus-4-6", "chain": {"manual_override": None}}
        task = asyncio.create_task(dispatch_model_hub_rpc(service, operation, payload))
        try:
            assert await asyncio.to_thread(blocked.wait, 2)
            assert not task.done() and owner not in disk_threads
        finally:
            release.set()
            await asyncio.wait_for(task, 3)
        assert set(recovery_threads) == {owner}

    asyncio.run(exercise())


def test_delayed_observation_disk_does_not_own_routing_lock(tmp_path, monkeypatch):
    src = _hub_source("src_slowdisk")
    service, store, adapter, _ = _background(tmp_path, [src])
    started, finish = threading.Event(), threading.Event()
    save = service._listing_file.save

    def delayed(listings):
        started.set()
        assert finish.wait(5)
        save(listings)

    monkeypatch.setattr(service._listing_file, "save", delayed)

    async def exercise():
        pending = asyncio.create_task(service.refresh_source(src.id))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            await asyncio.wait_for(service._mutation_lock.acquire(), 1)
            service._mutation_lock.release()
            assert not pending.done()
        finally:
            finish.set()
            await pending

    asyncio.run(exercise())
