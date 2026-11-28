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
    admitted = service._invocation_resolution(store.load(), "codex", mid, "hub")
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


@pytest.mark.parametrize("binding", ["unchanged", "unassigned", "replaced", "legacy", "legacy_unassigned"])
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
            "modalities": {"input": ["text"], "output": ["text"]}}}},
    })
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
        gateway = ModelHubTurnGateway(restarted)
        router = ModelHubRuntimeRouter(service=restarted, turn_gateway=gateway, overlay_path=tmp_path / "overlay.json")
        context = SimpleNamespace(platform_specific={"agent_session_target": {"id": sid}})
        try:
            launch = await resolve_model_hub_launch(SimpleNamespace(model_hub_runtime=router), "codex", mid,
                context=context, process_scope=sid, turn_id="turn_saved_offer")
            async with aiohttp.ClientSession(trust_env=False) as client:
                async with client.post(f"{launch.gateway_base_url}/v1/responses",
                        headers={"Authorization": f"Bearer {launch.gateway_token}",
                                 "x-codex-turn-metadata": json.dumps(launch.gateway_request_metadata)},
                        json={"model": mid, "input": "test", "stream": False}) as response:
                    assert response.status == 200, await response.text()
        finally:
            await gateway.close()

    if binding in ("unchanged", "legacy"):
        new_adapter.outcomes.append(RawCallOutcome(kind=RawOutcomeKind.SUCCESS, http_status=200,
            error_code=None, redacted_message=None, stream_started=False, model_id=mid, source_id=src.id))
        asyncio.run(run_saved_session())
        assert new_adapter.invocations == [(src.id, mid)]
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
