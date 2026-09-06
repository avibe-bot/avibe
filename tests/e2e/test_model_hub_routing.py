"""API-feasible Model Hub routing and fallback E2E scenarios."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import pytest

from core.handlers.model_hub.adapter import RawOutcomeKind
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from core.run_settlement import SETTLED_BY_TERMINAL_RESULT
from tests.e2e.drivers.mock_llm_upstream import MockLLMUpstream
from tests.e2e.test_model_hub_migration import _write
from tests.e2e.test_model_hub_runtime import (
    _engine_app,
    _install_engine,
    _isolated_engine_adapter,
)
from config.v2_config import ModelHubBackendModelConfig, ModelHubConfig, ModelHubRouteConfig, ModelHubRouteHopConfig
from tests.e2e.test_model_hub_sources import (
    MENU_MODEL,
    SYNTHETIC_API_KEY,
    _configure_protocol,
    _create_source,
)
from tests.scenario_harness.model_hub import (
    MemoryModelHubStore,
    ModelHubScenarioAdapter,
    ScenarioCallResult,
    config_with_sources,
    fixed_model,
    service_for,
    source,
    source_model,
)


pytestmark = pytest.mark.e2e_model_hub


def _real_route_service(tmp_path, adapter, mocks, protocol, inventory, *, manual=False, backend="opencode"):
    target = "vendor/unlisted-route-target" if backend == "opencode" else fixed_model(backend)
    menu = "vendor/menu-route-model" if manual else target
    sources = []
    for index, mock in enumerate(mocks):
        models = [target] if index else inventory
        mock.configure(protocol=protocol, models=[{"id": model} for model in models])
        item = source(f"src_gateway{index:03d}", models, vendor="custom", protocol=protocol)
        item.base_url = mock.url + ("/v1" if protocol == "openai_responses" else "")
        item.credential_ref = adapter.state_store.store_api_key(
            f"sk-synthetic-gateway-{index}", vendor="custom", protocol=protocol, base_url=item.base_url,
        )
        sources.append(item)
    config = config_with_sources(sources, backend=backend, menu_model=menu)
    agent = config.agents[backend]
    if backend == "opencode":
        agent.menu.checked = [menu]
        agent.models = [ModelHubBackendModelConfig(
            id=menu, origin="manual", native_protocol="anthropic" if protocol == "anthropic" else "openai_responses",
        )]
    agent.sources.order = [sources[0].id]
    if manual:
        agent.routes[menu] = ModelHubRouteConfig(hops=(ModelHubRouteHopConfig(sources[0].id, target),))
    else:
        agent.routes.pop(menu)
    service = service_for(tmp_path, MemoryModelHubStore(config), adapter)
    return service, menu, target, sources


@pytest.mark.parametrize("protocol", ["openai_responses", "openai_chat", "anthropic"])
@pytest.mark.parametrize("inventory", [[], ["unrelated-listed-model"]], ids=["empty", "incomplete"])
@pytest.mark.parametrize("manual", [False, True], ids=["passthrough", "manual"])
@pytest.mark.parametrize("stream", [False, True], ids=["buffered", "streaming"])
def test_mh_routing_008_real_gateway_cpa_exact_model_and_source(tmp_path, monkeypatch, protocol, inventory, manual, stream):
    """MH-ROUTING-008: real gateway/adapter/pinned CPA forwards exact unlisted API-key targets without source fallback."""
    async def exercise(adapter, first, second):
        service, menu, target, sources = _real_route_service(tmp_path, adapter, [first, second], protocol, inventory, manual=manual)
        await service.runtime_start()
        connection = adapter.supervisor.client_if_running().connection
        config_path = next(adapter.state_store.root.glob("instances/*/config.yaml"))
        before = (config_path.read_bytes(), config_path.stat().st_mtime_ns)
        gateway = ModelHubTurnGateway(service)
        base_url, token = await gateway.endpoint(
            "opencode", process_scope=str(tmp_path), turn_id="turn_routing_exact", requested_model_id=menu,
            resolved_model_id=target, source_id=sources[0].id,
        )
        paths = {"anthropic": "messages", "openai_responses": "responses", "openai_chat": "chat/completions"}
        prompt = "Original request: \u4e2d\u6587"
        payload = {"model": menu, "stream": stream}
        if protocol == "openai_responses":
            payload["input"] = prompt
        else:
            payload.update(max_tokens=32, messages=[{"role": "user", "content": prompt}])
        first.reset_requests()
        second.reset_requests()
        try:
            async with aiohttp.ClientSession(trust_env=False) as client:
                async with client.post(f"{base_url}/v1/{paths[protocol]}", json=payload,
                                       headers={"Authorization": f"Bearer {token}"}) as response:
                    raw = await asyncio.wait_for(response.read(), timeout=15)
                    assert response.status == 200, raw
                    assert b"mock response" in raw
        finally:
            await gateway.close()
        captured = [row for row in first.requests() if row["path"] == f"/v1/{paths[protocol]}"]
        assert len(captured) == 1, first.requests()
        assert captured[0]["body"]["model"] == target
        assert prompt in json.dumps(captured[0]["body"], ensure_ascii=False)
        assert not [row for row in second.requests() if row["path"] == f"/v1/{paths[protocol]}"]
        assert [row.id for row in service.store.load().sources[0].models] == inventory
        assert adapter.state_store.get_source(sources[0].id).model_ids == tuple(inventory)
        assert (config_path.read_bytes(), config_path.stat().st_mtime_ns) == before
        assert adapter.supervisor.client_if_running().connection == connection
        assert service.agent_chain("opencode", menu)["route_origin"] == ("manual" if manual else "passthrough")

    with MockLLMUpstream() as first, MockLLMUpstream() as second, _isolated_engine_adapter(tmp_path, monkeypatch) as adapter:
        asyncio.run(exercise(adapter, first, second))


@pytest.mark.parametrize("protocol", ["openai_responses", "openai_chat", "anthropic"])
@pytest.mark.parametrize("inventory", [[], ["unrelated-listed-model"]], ids=["empty", "incomplete"])
def test_mh_routing_012_persisted_dormant_opencode_override_reaches_exact_source_and_target(
    tmp_path, monkeypatch, protocol, inventory,
):
    """MH-ROUTING-012: resolver-selectable dormant routes are registered without expanding the menu or inventory."""
    async def exercise(adapter, first, second):
        arranged, dormant, target, sources = _real_route_service(
            tmp_path, adapter, [first, second], protocol, inventory, manual=True,
        )
        config = arranged.store.load()
        catalog = "vendor/independent-catalog-model"
        agent = config.agents["opencode"]
        agent.menu.checked = [catalog]
        agent.models = [ModelHubBackendModelConfig(
            id=catalog, origin="manual",
            native_protocol="anthropic" if protocol == "anthropic" else "openai_responses",
        )]
        persisted = tmp_path / "persisted-dormant-route.json"
        persisted.write_text(json.dumps(config.to_payload()), encoding="utf-8")
        loaded = ModelHubConfig.from_payload(json.loads(persisted.read_text(encoding="utf-8")))
        assert dormant in loaded.agents["opencode"].routes
        assert dormant not in {model.id for model in loaded.agents["opencode"].models}
        assert loaded.agents["opencode"].menu.checked == [catalog]
        agent_before = loaded.agents["opencode"].to_payload()
        inventory_before = [item.to_payload()["models"] for item in loaded.sources]
        persisted_before = persisted.read_bytes()
        service = service_for(tmp_path, MemoryModelHubStore(loaded), adapter)
        await service.runtime_start()
        connection = adapter.supervisor.client_if_running().connection
        config_path = next(adapter.state_store.root.glob("instances/*/config.yaml"))
        engine_before = (config_path.read_bytes(), config_path.stat().st_mtime_ns)
        gateway = ModelHubTurnGateway(service)
        base_url, token = await gateway.endpoint(
            "opencode", process_scope=str(tmp_path), turn_id="turn_dormant_route",
            requested_model_id=dormant, resolved_model_id=target, source_id=sources[0].id,
        )
        paths = {"anthropic": "messages", "openai_responses": "responses", "openai_chat": "chat/completions"}
        payload = {"model": dormant, "stream": False}
        prompt = "Persisted route request: \u4e2d\u6587"
        if protocol == "openai_responses":
            payload["input"] = prompt
        else:
            payload.update(max_tokens=32, messages=[{"role": "user", "content": prompt}])
        first.reset_requests()
        second.reset_requests()
        try:
            async with aiohttp.ClientSession(trust_env=False) as client:
                async with client.post(f"{base_url}/v1/{paths[protocol]}", json=payload,
                                       headers={"Authorization": f"Bearer {token}"}) as response:
                    raw = await asyncio.wait_for(response.read(), timeout=15)
                    assert response.status == 200, raw
                    assert b"mock response" in raw
        finally:
            await gateway.close()
        captured = [row for row in first.requests() if row["path"] == f"/v1/{paths[protocol]}"]
        assert len(captured) == 1, first.requests()
        assert captured[0]["body"]["model"] == target
        assert _request_credential(captured[0]) == "sk-synthetic-gateway-0"
        assert prompt in json.dumps(captured[0]["body"], ensure_ascii=False)
        assert not [row for row in second.requests() if row["method"] == "POST"]
        assert service.store.load().agents["opencode"].to_payload() == agent_before
        assert [item.to_payload()["models"] for item in service.store.load().sources] == inventory_before
        assert adapter.state_store.get_source(sources[0].id).model_ids == tuple(inventory)
        assert (config_path.read_bytes(), config_path.stat().st_mtime_ns) == engine_before
        assert adapter.supervisor.client_if_running().connection == connection
        assert persisted.read_bytes() == persisted_before
        assert service.agent_chain("opencode", dormant)["route_origin"] == "manual"
        (tmp_path / "received-dormant-route.json").write_text(json.dumps({
            "dormant_model": dormant, "source_id": sources[0].id, "target": target,
            "inventory": inventory, "menu": agent_before, "received": captured,
        }, indent=2), encoding="utf-8")

    with MockLLMUpstream() as first, MockLLMUpstream() as second, _isolated_engine_adapter(tmp_path, monkeypatch) as adapter:
        asyncio.run(exercise(adapter, first, second))


@pytest.mark.parametrize("protocol", ["openai_responses", "openai_chat", "anthropic"])
@pytest.mark.parametrize("disconnect", [False, True], ids=["complete", "client-close"])
def test_mh_routing_009_save_drains_active_stream_without_truncation_or_deadlock(tmp_path, monkeypatch, protocol, disconnect):
    """MH-ROUTING-009: Save waits for real stream completion or client close, then serves the new target without deadlock."""
    async def exercise(adapter, mock):
        service, menu, target, sources = _real_route_service(tmp_path, adapter, [mock], protocol, [])
        await service.runtime_start()
        old_connection = adapter.supervisor.client_if_running().connection
        gateway = ModelHubTurnGateway(service)
        base_url, token = await gateway.endpoint(
            "opencode", process_scope=str(tmp_path), turn_id="turn_active_save", requested_model_id=menu,
            resolved_model_id=target, source_id=sources[0].id,
        )
        path = {"anthropic": "messages", "openai_responses": "responses", "openai_chat": "chat/completions"}[protocol]
        terminal = {"anthropic": b"message_stop", "openai_responses": b"response.completed", "openai_chat": b"[DONE]"}[protocol]
        payload = {"model": menu, "stream": True}
        if protocol == "openai_responses":
            payload["input"] = "save during output"
        else:
            payload.update(max_tokens=32, messages=[{"role": "user", "content": "save during output"}])
        mock.configure(stream="pause_after_first_output")
        mock.reset_requests()
        save = None
        try:
            async with aiohttp.ClientSession(trust_env=False) as client:
                async with client.post(f"{base_url}/v1/{path}", json=payload,
                                       headers={"Authorization": f"Bearer {token}"}) as response:
                    assert response.status == 200
                    prefix = b""
                    while b"mock response" not in prefix:
                        prefix += await asyncio.wait_for(response.content.readuntil(b"\n\n"), timeout=10)
                    next_target = "vendor/next-unlisted-target"
                    save = asyncio.create_task(service.set_agent_chain("opencode", menu, {
                        "hops": [{"source_id": sources[0].id, "model_id": next_target}],
                    }))
                    await asyncio.sleep(0.05)
                    assert not save.done(), "Save must wait for the active transport to finish"
                    assert adapter.supervisor.client_if_running().connection == old_connection
                    if disconnect:
                        response.close()
                        saved = await asyncio.wait_for(save, timeout=15)
                        mock.configure(stream="healthy")
                    else:
                        mock.configure(stream="healthy")
                        saved, tail = await asyncio.wait_for(asyncio.gather(save, response.read()), timeout=15)
                        assert terminal in prefix + tail
                    assert saved["chain"]["manual_override"] == {"hops": [{"source_id": sources[0].id, "model_id": next_target}]}
                async with client.post(f"{base_url}/v1/{path}", json={**payload, "stream": False},
                                       headers={"Authorization": f"Bearer {token}"}) as response:
                    raw = await asyncio.wait_for(response.read(), timeout=10)
                    assert response.status == 200, raw
            captured = [row["body"]["model"] for row in mock.requests() if row["path"] == f"/v1/{path}"]
            assert captured == [target, next_target]
            assert service.store.load().sources[0].models == []
        finally:
            mock.configure(stream="healthy")
            if save is not None and not save.done():
                save.cancel()
                await asyncio.gather(save, return_exceptions=True)
            await gateway.close()

    with MockLLMUpstream() as mock, _isolated_engine_adapter(tmp_path, monkeypatch) as adapter:
        asyncio.run(exercise(adapter, mock))


@pytest.mark.parametrize("protocol", ["openai_responses", "openai_chat", "anthropic"])
@pytest.mark.parametrize("legacy_empty", [False, True], ids=["absent", "legacy-empty"])
def test_mh_routing_013_legacy_empty_inherits_matching_and_exact_passthrough_through_real_cpa(
    tmp_path, monkeypatch, protocol, legacy_empty,
):
    """MH-ROUTING-013: empty and absent loaded routes share matching, passthrough and stable registration."""
    async def exercise(adapter, mocks):
        backend = {"openai_responses": "codex", "openai_chat": "opencode", "anthropic": "claude"}[protocol]
        arranged, _, _, sources = _real_route_service(tmp_path, adapter, mocks, protocol, [], backend=backend)
        config = arranged.store.load()
        agent = config.agents[backend]
        if backend == "opencode":
            agent.models = [ModelHubBackendModelConfig(id=model, origin="manual", native_protocol="openai_responses")
                            for model in ("vendor/inherited-known", "vendor/inherited-unknown", "vendor/manual-control")]
            agent.menu.checked = [item.id for item in agent.models]
        known, unknown, manual_menu = [item.id for item in agent.models][:3]
        manual_target = "vendor/unlisted-manual-control"
        inventories = [["unrelated-inventory-model"], [known], []]
        for item, upstream, inventory in zip(sources, mocks, inventories, strict=True):
            item.models = [source_model(model) for model in inventory]
            upstream.configure(protocol=protocol, models=[{"id": model} for model in inventory])
        agent.sources.order = [sources[0].id, sources[1].id]
        agent.routes = {manual_menu: ModelHubRouteConfig(hops=(ModelHubRouteHopConfig(sources[2].id, manual_target),))}
        payload = config.to_payload()
        if legacy_empty:
            payload["agents"][backend]["routes"].update({known: {"hops": []}, unknown: {"hops": []}})
        persisted = tmp_path / "persisted-inheritance.json"
        persisted.write_text(json.dumps(payload), encoding="utf-8")
        original_bytes = persisted.read_bytes()
        loaded = ModelHubConfig.from_payload(json.loads(original_bytes))
        absent_payload = json.loads(original_bytes)
        for model in (known, unknown):
            absent_payload["agents"][backend]["routes"].pop(model, None)
        assert loaded.to_payload() == ModelHubConfig.from_payload(absent_payload).to_payload()
        assert known not in loaded.agents[backend].routes and unknown not in loaded.agents[backend].routes
        manual = {"hops": [{"source_id": sources[2].id, "model_id": manual_target}]}
        service = service_for(tmp_path, MemoryModelHubStore(loaded), adapter)
        await service.runtime_start()
        connection = adapter.supervisor.client_if_running().connection
        config_path = next(adapter.state_store.root.glob("instances/*/config.yaml"))
        engine_before = (config_path.read_bytes(), config_path.stat().st_mtime_ns)
        registration = [adapter.state_store.get_source(item.id) for item in sources]
        source_before = [item.to_payload() for item in service.store.load().sources]
        catalog_before = service.store.load().agents[backend].to_payload()
        gateway = ModelHubTurnGateway(service)
        path = {"anthropic": "messages", "openai_responses": "responses", "openai_chat": "chat/completions"}[protocol]
        evidence = []
        try:
            async with aiohttp.ClientSession(trust_env=False) as client:
                for order in ([0, 1], [1, 0]):
                    ordered_ids = [sources[index].id for index in order]
                    if service.store.load().agents[backend].sources.order != ordered_ids:
                        await service.set_agent_sources(backend, {"order": ordered_ids})
                    for model, selected, target, origin in (
                        (known, 1, known, "automatic"),
                        (unknown, order[0], unknown, "passthrough"),
                        (manual_menu, 2, manual_target, "manual"),
                    ):
                        chain = service.agent_chain(backend, model)
                        assert chain["manual_override"] == (manual if origin == "manual" else None)
                        assert chain["route_origin"] == origin
                        expected_chain = ([(sources[index].id, unknown) for index in order] if origin == "passthrough"
                                          else [(sources[selected].id, target)])
                        assert [(hop["source_id"], hop["model_id"]) for hop in chain["chain"]] == expected_chain
                        base_url, token = await gateway.endpoint(
                            backend, process_scope=f"{tmp_path}/{len(evidence)}", turn_id=f"turn_inherited_{len(evidence)}",
                            requested_model_id=model, resolved_model_id=target, source_id=sources[selected].id,
                        )
                        # Prepared Claude/Codex launches send the resolved target;
                        # OpenCode requests its menu id through the native overlay.
                        request = {"model": model if backend == "opencode" else target, "stream": False}
                        prompt = "Inherited exact route: \u4e2d\u6587"
                        if protocol == "openai_responses":
                            request["input"] = prompt
                        else:
                            request.update(max_tokens=32, messages=[{"role": "user", "content": prompt}])
                        for upstream in mocks:
                            upstream.reset_requests()
                        async with client.post(f"{base_url}/v1/{path}", json=request,
                                               headers={"Authorization": f"Bearer {token}"}) as response:
                            raw = await asyncio.wait_for(response.read(), timeout=15)
                            assert response.status == 200, raw
                            assert b"mock response" in raw
                        received = [row for row in mocks[selected].requests() if row["path"] == f"/v1/{path}"]
                        assert len(received) == 1
                        assert received[0]["body"]["model"] == target
                        assert _request_credential(received[0]) == f"sk-synthetic-gateway-{selected}"
                        assert prompt in json.dumps(received[0]["body"], ensure_ascii=False)
                        assert not [row for index, upstream in enumerate(mocks) if index != selected
                                    for row in upstream.requests() if row["method"] == "POST"]
                        assert [item.to_payload() for item in service.store.load().sources] == source_before
                        assert [adapter.state_store.get_source(item.id) for item in sources] == registration
                        assert [adapter.state_store.get_source(item.id).model_ids for item in sources] == [tuple(value) for value in inventories]
                        assert (config_path.read_bytes(), config_path.stat().st_mtime_ns) == engine_before
                        assert adapter.supervisor.client_if_running().connection == connection
                        actual_agent = service.store.load().agents[backend].to_payload()
                        assert actual_agent == {**catalog_before, "sources": {"order": ordered_ids}}
                        assert persisted.read_bytes() == original_bytes
                        evidence.append({"requested_model": model, "gateway_model": request["model"], "source_id": sources[selected].id,
                                         "route_origin": origin, "default_order": ordered_ids,
                                         "received": {"path": received[0]["path"], "model": received[0]["body"]["model"],
                                                      "synthetic_key": _request_credential(received[0])}})
        finally:
            await gateway.close()
            (tmp_path / "received-inherited-routes.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")

    with MockLLMUpstream() as first, MockLLMUpstream() as second, MockLLMUpstream() as outside, \
            _isolated_engine_adapter(tmp_path, monkeypatch) as adapter:
        asyncio.run(exercise(adapter, [first, second, outside]))


@pytest.mark.parametrize("protocol", ["openai_responses", "openai_chat", "anthropic"])
def test_mh_routing_010_model_not_found_stays_request_scoped_through_real_gateway_cpa(tmp_path, monkeypatch, protocol):
    """MH-ROUTING-010: an unlisted-model error does not poison source health or retry another source."""
    async def exercise(adapter, first, second):
        backend = {"openai_responses": "codex", "openai_chat": "opencode", "anthropic": "claude"}[protocol]
        service, menu, target, sources = _real_route_service(tmp_path, adapter, [first, second], protocol, [], backend=backend)
        sources[1].models = []
        second.configure(models=[])
        service.store.load().agents[backend].sources.order = [item.id for item in sources]
        await service.runtime_start()
        before = [item.state.to_payload() for item in service.store.load().sources]
        gateway = ModelHubTurnGateway(service)
        base_url, token = await gateway.endpoint(
            backend, process_scope=str(tmp_path), turn_id="turn_model_error", requested_model_id=menu,
            resolved_model_id=target, source_id=sources[0].id,
        )
        path = {"anthropic": "messages", "openai_responses": "responses", "openai_chat": "chat/completions"}[protocol]
        payload = {"model": menu, "stream": False}
        if protocol == "openai_responses":
            payload["input"] = "missing model"
        else:
            payload.update(max_tokens=32, messages=[{"role": "user", "content": "missing model"}])
        first.configure(model_errors={target: "model_not_found"})
        first.reset_requests()
        second.reset_requests()
        try:
            async with aiohttp.ClientSession(trust_env=False) as client:
                async with client.post(f"{base_url}/v1/{path}", json=payload,
                                       headers={"Authorization": f"Bearer {token}"}) as response:
                    raw = await asyncio.wait_for(response.read(), timeout=10)
                    assert response.status == 400, raw
                    assert json.loads(raw)["error"]["code"] == "upstream_request_invalid"
                assert [item.state.to_payload() for item in service.store.load().sources] == before
                assert service.agent_chain(backend, menu)["route_origin"] == "passthrough"
                gateway.correlation.settle(
                    "turn_model_error", settled_by=SETTLED_BY_TERMINAL_RESULT,
                    ts="2026-09-05T18:00:00+00:00",
                )
                failed = service.get_model_provenance(backend, menu)
                if backend == "opencode":
                    # The shared OpenCode process intentionally has no exact turn discriminator.
                    assert failed is None
                else:
                    assert failed is not None
                    assert failed["turn_id"] == "turn_model_error"
                    assert failed["terminal_error"] == {
                        "source_id": sources[0].id,
                        "configured_model_id": target,
                        "channel": "hub",
                        "reason": "invalid_parameter",
                        "stream_started": False,
                        "http_status": 404,
                        "upstream_error_code": "not_found_error" if protocol == "anthropic" else "model_not_found",
                    }
                first.configure(model_errors={})
                base_url, token = await gateway.endpoint(
                    backend, process_scope=str(tmp_path), turn_id="turn_model_success",
                    requested_model_id=menu, resolved_model_id=target, source_id=sources[0].id,
                )
                async with client.post(f"{base_url}/v1/{path}", json=payload,
                                       headers={"Authorization": f"Bearer {token}"}) as response:
                    raw = await asyncio.wait_for(response.read(), timeout=10)
                    assert response.status == 200, raw
                gateway.correlation.settle(
                    "turn_model_success", settled_by=SETTLED_BY_TERMINAL_RESULT,
                    ts="2026-09-05T18:00:01+00:00",
                )
                latest = service.get_model_provenance(backend, menu)
                if backend == "opencode":
                    assert latest is None
                else:
                    assert latest is not None
                    assert latest["turn_id"] == "turn_model_success"
                    assert latest["terminal_error"] is None
                    assert latest["served"]["source_id"] == sources[0].id
                assert service.provenance.get("turn_model_error") == failed
            assert [row["body"]["model"] for row in first.requests() if row["path"] == f"/v1/{path}"] == [target, target]
            assert not [row for row in second.requests() if row["path"] == f"/v1/{path}"]
        finally:
            await gateway.close()

    with MockLLMUpstream() as first, MockLLMUpstream() as second, _isolated_engine_adapter(tmp_path, monkeypatch) as adapter:
        asyncio.run(exercise(adapter, first, second))


@pytest.mark.parametrize("settings_scope", ["home", "project", "local"])
def test_mh_claude_launch_001_settings_cannot_bypass_the_configured_route(
    model_hub_app_factory, mock_llm_upstream, settings_scope,
) -> None:
    """MH-CLAUDE-LAUNCH-001: native settings cannot change a Hub turn's egress."""

    import claude_agent_sdk

    binary = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    assert binary.is_file(), "The installed Claude SDK must supply its CLI"
    menu_model = "claude-opus-5"
    routed_model = "gpt-6-astra"
    native_key = "sk-native-claude-settings-fixture"
    with MockLLMUpstream() as native, _engine_app(model_hub_app_factory) as app:
        _configure_protocol(native, "anthropic", models=[{"id": menu_model}])
        cwd = app.home if settings_scope == "home" else app.home / "project"
        filename = "settings.local.json" if settings_scope == "local" else "settings.json"
        native_settings = cwd / ".claude" / filename
        native_payload = json.dumps({"env": {
            "ANTHROPIC_BASE_URL": native.url,
            "ANTHROPIC_AUTH_TOKEN": native_key,
            "ANTHROPIC_API_KEY": native_key,
            "ANTHROPIC_CUSTOM_HEADERS": f"Authorization: Bearer {native_key}",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "CLAUDE_CODE_USE_VERTEX": "1",
            "CLAUDE_CODE_USE_FOUNDRY": "1",
        }})
        _write(native_settings, native_payload)
        configured = app.client.post("/api/config", {
            "agents": {"claude": {"enabled": True, "cli_path": str(binary)}},
        })
        assert configured.status == 200, configured.json()
        _install_engine(app)
        _configure_protocol(mock_llm_upstream, "openai_responses", models=[{"id": routed_model}])
        supplied, _ = _create_source(
            app, mock_llm_upstream, protocol="openai_responses",
            extra={"base_url": mock_llm_upstream.url + "/v1"},
        )
        mode = app.client.patch("/api/models/agents/claude/mode", {"mode": "hub"})
        assert mode.status == 200, mode.json()
        chain = app.client.put(f"/api/models/agents/claude/chain?model={menu_model}", {
            "hops": [{"source_id": supplied["id"], "model_id": routed_model}],
        })
        assert chain.status == 200, chain.json()
        agent = app.client.post("/api/agents", {
            "name": "claude-hub-settings-e2e", "backend": "claude",
            "model": menu_model, "system_prompt": "Reply with the supplied response.",
        })
        assert agent.status == 200, agent.json()
        project = app.client.post("/api/projects", {
            "folder_path": str(cwd), "display_name": "Claude Hub settings E2E",
        })
        assert project.status == 201, project.json()
        session = app.client.post("/api/sessions", {
            "project_id": project.json()["id"], "agent_name": "claude-hub-settings-e2e",
            "model": menu_model,
        })
        assert session.status == 201, session.json()
        session_id = session.json()["id"]
        mock_llm_upstream.reset_requests()
        native.reset_requests()
        sent = app.client.post(f"/api/sessions/{session_id}/messages", {"text": "hello"})
        assert sent.status == 202, sent.json()
        result = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            transcript = app.client.get(f"/api/sessions/{session_id}/messages?tail=1")
            assert transcript.status == 200, transcript.json()
            result = next((row for row in transcript.json()["messages"] if row["type"] == "result"), None)
            if result is not None or any(item["path"].startswith("/v1/messages") for item in native.requests()):
                break
            time.sleep(0.1)
        native_calls = [item for item in native.requests() if item["path"].startswith("/v1/messages")]
        assert native_calls == [], "Claude bypassed Model Hub using native settings"
        captured = [item for item in mock_llm_upstream.requests() if item["path"] == "/v1/responses"]
        assert captured, app.diagnostics()
        assert all(item["body"]["model"] == routed_model for item in captured)
        assert all(_request_credential(item) == SYNTHETIC_API_KEY for item in captured)
        assert result is not None, app.diagnostics()
        assert result["text"] == "mock response"
        provenance = app.client.get(f"/api/models/turns/{result['metadata']['turn_id']}/provenance")
        assert provenance.status == 200, provenance.json()
        attribution = provenance.json()["provenance"]
        assert attribution["requested_model_id"] == menu_model
        assert attribution["outcome"] == "served"
        assert attribution["served"]["source_id"] == supplied["id"]
        assert attribution["served"]["configured_model_id"] == routed_model
        assert attribution["served"]["channel"] == "hub"
        assert native_settings.read_text() == native_payload


def _seed_codex_native_login(home) -> None:
    _write(
        home / ".codex" / "auth.json",
        (
            '{"auth_mode":"chatgpt","tokens":'
            '{"access_token":"codex-native-e2e-123456"}}'
        ),
    )
    _write(
        home / ".codex" / "config.toml",
        'cli_auth_credentials_store = "file"\n',
    )


def test_d1_subscription_added_after_api_key_is_appended_at_tail(
    mock_llm_upstream,
    model_hub_app,
) -> None:
    """D1: current placement appends a later native subscription."""

    # D-1 remains open: current shipped behavior appends at tail, so the API
    # key precedes and can burn before the later subscription.
    _configure_protocol(
        mock_llm_upstream,
        "openai_responses",
        models=[{"id": "gpt-5.3-codex"}],
    )
    api_source, _ = _create_source(
        model_hub_app,
        mock_llm_upstream,
        protocol="openai_responses",
        nonce="scn_d100000000000001",
    )
    _seed_codex_native_login(model_hub_app.home)
    scan = model_hub_app.client.post("/api/models/migration/scan", {})
    scan_body = scan.json()
    assert scan.status == 200, scan_body
    [native_item] = [
        item
        for item in scan_body["scan"]["items"]
        if item["kind"] == "oauth_native"
    ]
    applied = model_hub_app.client.post(
        "/api/models/migration/apply", {"item_ids": [native_item["id"]]}
    )
    applied_body = applied.json()
    assert applied.status == 200, applied_body
    native_source = next(
        source
        for source in applied_body["sources"]
        if (
            source["vendor"] == "openai"
            and source["kind"] == "subscription"
            and source["supply_channel"] == "native_cli"
        )
    )

    mode = model_hub_app.client.patch(
        "/api/models/agents/codex/mode", {"mode": "hub"}
    )
    assert mode.status == 200, mode.json()

    agent = model_hub_app.client.get(
        "/api/models/agents/codex/sources"
    )
    assert agent.status == 200, agent.json()
    agent_body = agent.json()
    assert agent_body["agent"]["sources"]["order"] == [
        api_source["id"],
        native_source["id"],
    ]


def _prepare_probe_chain(
    app,
    first_upstream: MockLLMUpstream,
    second_upstream: MockLLMUpstream | None = None,
):
    _install_engine(app)
    _configure_protocol(
        first_upstream,
        "anthropic",
        models=[{"id": MENU_MODEL}],
    )
    first, _ = _create_source(
        app,
        first_upstream,
        nonce="scn_dprobe0000000001",
        vendor="anthropic",
        display_name="First probe source",
    )
    sources = [first]
    if second_upstream is not None:
        _configure_protocol(
            second_upstream,
            "anthropic",
            models=[{"id": MENU_MODEL}],
        )
        second, _ = _create_source(
            app,
            second_upstream,
            nonce="scn_dprobe0000000002",
            vendor="anthropic",
            display_name="Second probe source",
        )
        sources.append(second)
    mode = app.client.patch(
        "/api/models/agents/claude/mode", {"mode": "hub"}
    )
    assert mode.status == 200, mode.json()
    chain = app.client.put(
        f"/api/models/agents/claude/chain?model={MENU_MODEL}",
        {
            "hops": [
                {"source_id": source["id"], "model_id": MENU_MODEL}
                for source in sources
            ]
        },
    )
    assert chain.status == 200, chain.json()
    started = app.client.post("/api/models/runtime/start", {})
    assert started.status == 200, started.json()
    assert started.json()["runtime"]["status"]["health"] == "ok"
    first_upstream.reset_requests()
    if second_upstream is not None:
        second_upstream.reset_requests()
    return sources


def _probe(app):
    return app.client.post(
        "/api/models/agents/claude/probe", {"model": MENU_MODEL}
    )


def _source_by_id(app, source_id: str):
    sources = app.client.get("/api/models/sources")
    assert sources.status == 200, sources.json()
    return next(
        source
        for source in sources.json()["sources"]
        if source["id"] == source_id
    )


def _request_credential(request: dict) -> str | None:
    headers = request["headers"]
    if key := headers.get("x-api-key"):
        return key
    authorization = headers.get("authorization", "")
    return authorization.removeprefix("Bearer ") or None


def test_d2_healthy_hop_is_served_by_the_real_engine_probe(
    model_hub_app_factory,
    mock_llm_upstream,
) -> None:
    """D2 API subset: a healthy exact hop reaches the mock through the engine."""

    with _engine_app(model_hub_app_factory) as app:
        [source] = _prepare_probe_chain(app, mock_llm_upstream)
        response = _probe(app)
        body = response.json()
        assert response.status == 200, body
        assert body["probe"]["reachable"] is True
        assert body["probe"]["source_id"] == source["id"]
        assert body["probe"]["model_id"] == MENU_MODEL
        captured = [
            item
            for item in mock_llm_upstream.requests()
            if item["path"] == "/v1/messages"
        ]
        assert captured
        # The engine forwards Anthropic credentials as Bearer; x-api-key is
        # used only by Avibe's direct discovery probe path.
        assert all(
            item["headers"].get("authorization")
            == f"Bearer {SYNTHETIC_API_KEY}"
            for item in captured
        )
        assert all(
            item["headers"].get("anthropic-version") for item in captured
        )


def test_replaced_shared_key_keeps_source_identity_and_reaches_upstream_once(
    model_hub_app_factory,
    mock_llm_upstream,
) -> None:
    """A replaced duplicate key keeps each Source's own model registration."""

    route_model = "e2e-route-1"
    original_key = "sk-model-hub-e2e-original"
    replacement_key = "sk-model-hub-e2e-replacement"
    with _engine_app(model_hub_app_factory) as app:
        _install_engine(app)

        _configure_protocol(
            mock_llm_upstream,
            "anthropic",
            models=[{"id": MENU_MODEL}],
        )
        _create_source(
            app,
            mock_llm_upstream,
            nonce="scn_1818anchor000001",
            vendor="anthropic",
            display_name="Shared-key anchor",
            extra={"key": replacement_key},
        )

        _configure_protocol(
            mock_llm_upstream,
            "anthropic",
            models=[{"id": route_model}],
        )
        routed, _ = _create_source(
            app,
            mock_llm_upstream,
            nonce="scn_1818route0000001",
            vendor="anthropic",
            display_name="Replaced routed source",
            extra={"key": original_key},
        )
        _create_source(
            app,
            mock_llm_upstream,
            nonce="scn_1818sibling00001",
            vendor="anthropic",
            display_name="Shared-key sibling",
            extra={"key": replacement_key},
        )

        mode = app.client.patch(
            "/api/models/agents/claude/mode",
            {"mode": "hub"},
        )
        assert mode.status == 200, mode.json()
        chain = app.client.put(
            f"/api/models/agents/claude/chain?model={MENU_MODEL}",
            {
                "hops": [
                    {
                        "source_id": routed["id"],
                        "model_id": route_model,
                    }
                ]
            },
        )
        assert chain.status == 200, chain.json()
        started = app.client.post("/api/models/runtime/start", {})
        assert started.status == 200, started.json()
        first_port = started.json()["runtime"]["status"]["listening"][
            "port"
        ]

        original_ref = routed["credential_ref"]
        replaced = app.client.put(
            f"/api/models/sources/{routed['id']}/credential",
            {"key": replacement_key},
        )
        replaced_body = replaced.json()
        assert replaced.status == 200, replaced_body
        assert replaced_body["source"]["id"] == routed["id"]
        assert replaced_body["source"]["credential_ref"] != original_ref
        after_replace = app.client.get("/api/models/runtime/status")
        assert after_replace.status == 200, after_replace.json()
        replaced_port = after_replace.json()["runtime"]["status"][
            "listening"
        ]["port"]
        assert replaced_port != first_port

        app.restart_controller()
        mock_llm_upstream.reset_requests()
        response = _probe(app)
        body = response.json()
        assert response.status == 200, body
        captured = [
            item
            for item in mock_llm_upstream.requests()
            if item["path"] == "/v1/messages"
        ]
        assert body["probe"]["reachable"] is True, {
            "probe": body["probe"],
            "upstream_request_count": len(captured),
        }
        assert body["probe"]["source_id"] == routed["id"]
        assert body["probe"]["model_id"] == route_model
        assert len(captured) == 1
        assert _request_credential(captured[0]) == replacement_key
        assert _source_by_id(app, routed["id"])["state"] == {
            "status": "standby",
            "retry_at": None,
            "detail_key": None,
        }

        mock_llm_upstream.reset_requests()
        repeated = _probe(app)
        assert repeated.status == 200, repeated.json()
        assert repeated.json()["probe"]["reachable"] is True
        repeated_requests = [
            item
            for item in mock_llm_upstream.requests()
            if item["path"] == "/v1/messages"
        ]
        assert len(repeated_requests) == 1
        assert (
            _source_by_id(app, routed["id"])["state"]["retry_at"]
            is None
        )


def _assert_probe_cooldown_and_next_request_selection(
    *,
    app,
    first_upstream,
    second_upstream,
    auth_behavior: str,
    expected_detail: str,
    expected_reason: str,
    expected_delay: int,
) -> None:
    first, second = _prepare_probe_chain(
        app, first_upstream, second_upstream
    )
    first_upstream.configure(auth=auth_behavior)
    failed = _probe(app)
    failed_body = failed.json()
    assert failed.status == 200, failed_body
    assert failed_body["probe"]["reachable"] is False
    assert failed_body["probe"]["source_id"] == first["id"]
    first_state = _source_by_id(app, first["id"])["state"]
    assert first_state["status"] == "cooldown"
    assert first_state["detail_key"] == expected_detail
    retry_at = datetime.fromisoformat(first_state["retry_at"])
    delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
    assert expected_delay - 8 <= delay <= expected_delay + 2

    next_probe = _probe(app)
    next_body = next_probe.json()
    assert next_probe.status == 200, next_body
    assert next_body["probe"]["reachable"] is True
    assert next_body["probe"]["source_id"] == second["id"]
    events = app.client.get("/api/models/events?limit=10").json()["events"]
    assert any(
        event["kind"] == "cooldown"
        and event["from_source"] == first["id"]
        and event["reason"] == expected_reason
        for event in events
    )


def test_d3_rate_limit_moves_the_next_api_probe_to_hop_one(
    model_hub_app_factory,
    mock_llm_upstream,
) -> None:
    """D3 partial API evidence: cooldown written; next-request selection proven.

    The same-turn hop walk is not covered here; it requires the private turn
    gateway credential issued only to a live backend turn.
    """

    with MockLLMUpstream() as second_upstream:
        with _engine_app(model_hub_app_factory) as app:
            _assert_probe_cooldown_and_next_request_selection(
                app=app,
                first_upstream=mock_llm_upstream,
                second_upstream=second_upstream,
                auth_behavior="429",
                expected_detail="models.source.cooldown.rate_limited",
                expected_reason="rate_limited",
                expected_delay=60,
            )


def test_d4_quota_moves_the_next_api_probe_to_hop_one(
    model_hub_app_factory,
    mock_llm_upstream,
) -> None:
    """D4 partial API evidence: cooldown written; next-request selection proven.

    The same-turn hop walk is not covered here; it requires the private turn
    gateway credential issued only to a live backend turn.
    """

    with MockLLMUpstream() as second_upstream:
        with _engine_app(model_hub_app_factory) as app:
            _assert_probe_cooldown_and_next_request_selection(
                app=app,
                first_upstream=mock_llm_upstream,
                second_upstream=second_upstream,
                auth_behavior="quota_message",
                expected_detail="models.source.cooldown.quota_exhausted",
                expected_reason="quota_exhausted",
                expected_delay=300,
            )


def test_d5_nonrefreshable_401_marks_the_key_for_repair(
    model_hub_app_factory,
    mock_llm_upstream,
) -> None:
    """D5 API subset: a static key is revoked without an in-probe refresh."""

    with MockLLMUpstream() as second_upstream:
        with _engine_app(model_hub_app_factory) as app:
            first, second = _prepare_probe_chain(
                app, mock_llm_upstream, second_upstream
            )
            mock_llm_upstream.configure(auth="401")
            failed = _probe(app)
            assert failed.status == 200, failed.json()
            assert failed.json()["probe"]["reachable"] is False
            state = _source_by_id(app, first["id"])["state"]
            assert state == {
                "status": "needs_action",
                "retry_at": None,
                "detail_key": (
                    "models.source.needs_action.credential_revoked"
                ),
            }
            inference_requests = [
                item
                for item in mock_llm_upstream.requests()
                if item["path"] == "/v1/messages"
            ]
            assert len(inference_requests) == 1
            next_probe = _probe(app)
            assert next_probe.status == 200, next_probe.json()
            assert next_probe.json()["probe"]["source_id"] == second["id"]


def test_d6_server_error_ignores_retry_after_and_uses_flat_cooldown(
    model_hub_app_factory,
    mock_llm_upstream,
) -> None:
    """D6 API subset: a 5xx uses 30s despite Retry-After: 600."""

    # D-4 is open. The current contract deliberately records the flat 30s
    # server-error cooldown rather than honoring the upstream Retry-After.
    with MockLLMUpstream() as second_upstream:
        with _engine_app(model_hub_app_factory) as app:
            _assert_probe_cooldown_and_next_request_selection(
                app=app,
                first_upstream=mock_llm_upstream,
                second_upstream=second_upstream,
                auth_behavior="5xx",
                expected_detail="models.source.cooldown.server_error",
                expected_reason="server_error",
                expected_delay=30,
            )


def test_d10_strip_is_forwarded_and_persisted_for_the_exact_fallback_hop(
    tmp_path,
) -> None:
    """D10: the per-turn gateway records the exact hop whose effort it strips."""

    async def exercise() -> None:
        first = source(
            "src_d10first1",
            [source_model("relay-model", reasoning_efforts=("high",))],
            vendor="openai",
            protocol="openai_responses",
        )
        second = source(
            "src_d10second",
            [source_model("relay-model")],
            vendor="openai",
            protocol="openai_responses",
        )
        requested_model = fixed_model("codex")
        config = config_with_sources(
            [first, second],
            backend="codex",
            menu_model=requested_model,
            hops=(
                (first.id, "relay-model"),
                (second.id, "relay-model"),
            ),
        )
        adapter = ModelHubScenarioAdapter(
            invoke_results=(
                ScenarioCallResult(
                    RawOutcomeKind.HTTP_ERROR,
                    status=429,
                    error_code="rate_limit_error",
                ),
                ScenarioCallResult(RawOutcomeKind.SUCCESS, status=200),
            )
        )
        service = service_for(tmp_path, MemoryModelHubStore(config), adapter)
        gateway = ModelHubTurnGateway(service)
        turn_id = "turn_d10_strip"
        base_url, token = await gateway.endpoint(
            "codex",
            process_scope="/repo",
            turn_id=turn_id,
            requested_model_id=requested_model,
            resolved_model_id="relay-model",
            source_id=first.id,
        )
        try:
            async with aiohttp.ClientSession(trust_env=False) as client:
                response = await client.post(
                    f"{base_url}/v1/responses",
                    json={
                        "model": "relay-model",
                        "input": "ping",
                        "reasoning": {"effort": "high", "summary": "auto"},
                        "stream": False,
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status == 200
                await response.read()
        finally:
            await gateway.close()

        assert adapter.requests[0]["reasoning"] == {
            "effort": "high",
            "summary": "auto",
        }
        assert adapter.requests[1]["reasoning"] == {"summary": "auto"}
        gateway.correlation.settle(
            turn_id,
            settled_by=SETTLED_BY_TERMINAL_RESULT,
            ts="2026-09-03T06:00:00+00:00",
        )
        record = service.provenance.get(turn_id)
        assert record is not None
        assert record["failed_attempts"][0]["source_id"] == first.id
        assert record["served"] == {
            "source_id": second.id,
            "configured_model_id": "relay-model",
            "channel": "hub",
            "stripped_reasoning_efforts": ["high"],
            "declared_reasoning_efforts": [],
        }

    asyncio.run(exercise())
