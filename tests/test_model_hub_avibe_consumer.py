"""C-6 at the existing config, launch and HTTP/provenance owning boundaries.

Released fixtures previously fail backend admission. Existing CLI fixtures do not
protect a native-protocol in-process consumer, nullable capabilities, or an origin
header emitted before a slow stream and tied to the actual fallback attempt.
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from jsonschema import Draft7Validator, ValidationError
from referencing import Registry, Resource

from config.v2_config import (
    ModelHubBackendModelConfig,
    ModelHubConfig,
    ModelHubRouteConfig,
    ModelHubRouteHopConfig,
)
from core.handlers.model_hub.adapter import RawOutcomeKind
from core.handlers.model_hub.provenance import HopOrigin, SERVED_HOP_HEADER
from core.handlers.model_hub.service import ModelHubError
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from core.run_settlement import SETTLED_BY_TERMINAL_RESULT
from modules.agents.model_hub import (
    ModelHubRuntimeRouter,
    bind_persisted_launch,
    build_claude_hub_env,
    build_codex_hub_launch,
    claude_settings_for_launch,
    resolve_model_hub_launch,
)
from tests.test_model_hub_l3 import (
    InvokeHandle,
    LiveInvokeHandle,
    NOW,
    _canonicalize_fixed_test_routes,
    _outcome,
    _service,
    _source,
)


CONTRACTS = Path(__file__).parents[1] / "docs/plans/model-hub-contracts"
FIXTURES = Path(__file__).parent / "fixtures/model_hub"
PROTOCOL_ENDPOINTS = {
    "anthropic": "messages",
    "openai_chat": "chat/completions",
    "openai_responses": "responses",
}


def _validate(schema_name, value):
    resources = {}
    for path in CONTRACTS.glob("*.schema.json"):
        payload = json.loads(path.read_text())
        resources[payload.get("$id", path.name)] = Resource.from_contents(payload)
    schema = json.loads((CONTRACTS / schema_name).read_text())
    Draft7Validator(schema, registry=Registry().with_resources(resources.items())).validate(value)


def _avibe_service(tmp_path, sources, *, handles=()):
    service = _service(tmp_path, sources=sources, live_handles=list(handles))
    _canonicalize_fixed_test_routes(service)
    agent = service.store.config.agents["avibe"]
    agent.models = [ModelHubBackendModelConfig(id="menu-alias")]
    agent.sources.order = [source.id for source in sources if source.supply_channel == "hub"]
    agent.routes["menu-alias"] = ModelHubRouteConfig(tuple(
        ModelHubRouteHopConfig(source.id, source.models[0].id)
        for source in sources if source.supply_channel == "hub"
    ))
    service.store.requested_models["avibe"] = "menu-alias"
    return service


def _body(protocol, stream):
    if not stream:
        return json.dumps({"id": "fixture-answer", "content": "answer"}).encode()
    if protocol == "anthropic":
        return (
            b'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","usage":'
            b'{"input_tokens":2,"output_tokens":0}}}\n\n'
            b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"text_delta","text":"answer"}}\n\n'
            b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
            b'"usage":{"output_tokens":1}}\n\n'
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        )
    if protocol == "openai_chat":
        return (
            b'data: {"choices":[{"index":0,"delta":{"content":"answer"},"finish_reason":null}]}\n\n'
            b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
            b'data: [DONE]\n\n'
        )
    return (
        b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"answer"}\n\n'
        b'event: response.completed\ndata: {"type":"response.completed","response":'
        b'{"id":"resp_1","status":"completed","usage":{"input_tokens":2,"output_tokens":1}}}\n\n'
    )


def test_released_config_adds_only_empty_avibe_supply():
    """MH-AVIBE-003: preserve shipped native rows and add an empty Hub consumer."""
    released = json.loads((FIXTURES / "released_pre_avibe_consumer.json").read_text())
    before = copy.deepcopy(released)
    config = ModelHubConfig.from_payload(released)
    assert released == before
    assert set(config.agents) == {"claude", "codex", "opencode", "avibe"}
    for backend in ("claude", "codex", "opencode"):
        agent = config.agents[backend]
        original = before["agents"][backend]
        assert (agent.backend, agent.mode, agent.menu_kind, agent.sources.order) == (
            backend, original["mode"], original["menu_kind"], original["sources"]["order"],
        )
        assert {
            model_id: [(hop.source_id, hop.model_id) for hop in route.hops]
            for model_id, route in agent.routes.items()
        } == {
            model_id: [(hop["source_id"], hop["model_id"]) for hop in route["hops"]]
            for model_id, route in original["routes"].items()
        }
        assert [model.id for model in agent.models] == [model["id"] for model in original["models"]]
    assert config.agents["claude"].models[0].context_window == 100000
    assert config.agents["codex"].models[0].max_output_tokens == 4096
    assert config.agents["codex"].models[0].supports_tools is False
    assert config.agents["opencode"].menu.checked == []
    avibe = config.agents["avibe"]
    assert (avibe.mode, avibe.menu_kind, avibe.models, avibe.sources.order, avibe.routes) == (
        "hub", "fixed", [], [], {},
    )
    assert config.enabled is before["enabled"]
    assert ModelHubConfig.from_payload(config.to_payload()).to_payload() == config.to_payload()


def test_native_cli_and_direct_are_not_avibe_channels(tmp_path):
    """MH-AVIBE-004: neither invalid persisted state nor routing can select a CLI."""
    native = _source("src_native001", "Native", channel="native_cli", vendor="anthropic", protocol="anthropic")
    service = _avibe_service(tmp_path, [native])
    config = service.store.load()
    assert not ModelHubConfig.source_eligible_for_backend(native, "avibe")
    agent = config.agents["avibe"]
    agent.routes["menu-alias"] = ModelHubRouteConfig((ModelHubRouteHopConfig(native.id, "shared-model"),))
    chain = service.agent_chain("avibe", "menu-alias")
    assert chain["current"] is None
    assert chain["supply_state"] == "interrupted"
    with pytest.raises(ValueError, match="ineligible"):
        ModelHubConfig.from_payload(config.to_payload())
    agent.routes.clear()
    agent.mode = "direct"
    with pytest.raises(ValueError, match="must be hub"):
        ModelHubConfig.from_payload(config.to_payload())
    assert bind_persisted_launch(SimpleNamespace(), {
        "backend": "avibe", "channel": "native_cli", "source_id": native.id, "target_model": "shared-model",
    }) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", PROTOCOL_ENDPOINTS)
@pytest.mark.parametrize("known", [False, True])
async def test_hop_resolution_uses_primary_protocol_and_alias_capabilities(tmp_path, protocol, known):
    """MH-AVIBE-001: the launch boundary owns protocol, identity and planning metadata."""
    source = _source("src_primary01", "Primary", protocol=protocol, model_id="upstream-model")
    service = _avibe_service(tmp_path, [source])
    if known:
        service.store.config.agents["avibe"].models[0] = ModelHubBackendModelConfig(
            id="menu-alias", context_window=128000, max_output_tokens=4096,
            input_modalities=["text", "image"], supports_tools=True, supports_reasoning=False,
            reasoning_efforts=["high"],
        )
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="avibe:test", turn_id="turn-hop")
        _validate("hop-resolution.schema.json", hop)
        assert hop["protocol"] == protocol
        assert hop["provider"] == "openai"
        assert hop["base_url"].endswith("/avibe/v1")
        assert hop["runtime_model"] == "upstream-model"
        assert hop["source_id"] == source.id
        assert hop["capabilities"] == {
            "context_window": 128000 if known else None,
            "input_limit": None,
            "max_output_tokens": 4096 if known else None,
            "supports_tools": True if known else None,
            "supports_images": True if known else None,
            "supports_reasoning": False if known else None,
            "reasoning_efforts": [],
        }
        # Unknown is an explicit null, never an omitted field. Positive producer
        # checks alone miss a schema that accepts incomplete future responses.
        for field in (
            "context_window", "input_limit", "max_output_tokens", "supports_tools",
            "supports_images", "supports_reasoning", "reasoning_efforts",
        ):
            incomplete = copy.deepcopy(hop)
            incomplete["capabilities"].pop(field)
            with pytest.raises(ValidationError) as missing:
                _validate("hop-resolution.schema.json", incomplete)
            assert missing.value.validator == "required"
            assert list(missing.value.absolute_path) == ["capabilities"]
            assert missing.value.message == f"'{field}' is a required property"
        launch = await router.resolve("avibe", "menu-alias", process_scope="avibe:test", turn_id="turn-hop")
        assert hop["token"] not in repr(launch)
        assert build_claude_hub_env({"PATH": "/fixture"}, launch) == {"PATH": "/fixture"}
        assert build_codex_hub_launch(["fixture"], {}, launch) == (["fixture"], None)
        assert claude_settings_for_launch("{}", launch) == "{}"
        assert service.list_agents()[-1]["cli_present"] is False
        with pytest.raises(ModelHubError):
            await service.set_agent_mode("avibe", "direct")
        with pytest.raises(ModelHubError):
            await ModelHubRuntimeRouter(service=service).resolve("avibe", "menu-alias")
        with pytest.raises(ModelHubError):
            await resolve_model_hub_launch(SimpleNamespace(), "avibe", "menu-alias")
    finally:
        await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", PROTOCOL_ENDPOINTS)
@pytest.mark.parametrize("delivery", ["buffered", "stream", "settled"])
async def test_failover_origin_is_available_before_body_and_matches_served_attempt(tmp_path, protocol, delivery):
    """MH-AVIBE-002: HTTP headers identify the same fallback attempt that is persisted."""
    stream = delivery == "stream"
    fallback_protocol = "openai_responses" if protocol != "openai_responses" else "anthropic"
    first = _source("src_primary01", "Primary", vendor="custom", protocol=protocol)
    second = _source(
        "src_fallback01", "Fallback", vendor="anthropic", protocol=fallback_protocol, model_id="模型/β",
    )
    answer = b"{}" if delivery == "settled" else _body(protocol, stream)
    success = _outcome(RawOutcomeKind.SUCCESS, source_id=second.id, stream_started=stream)
    service = _avibe_service(tmp_path, [first, second], handles=[
        InvokeHandle(_outcome(RawOutcomeKind.HTTP_ERROR, status=429, code="rate_limit_error", source_id=first.id)),
        InvokeHandle(success) if delivery == "settled" else LiveInvokeHandle(success, (answer,)),
    ])
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="avibe:test", turn_id="turn-fallback")
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f'{hop["base_url"]}/{PROTOCOL_ENDPOINTS[protocol]}',
                headers={"Authorization": f'Bearer {hop["token"]}'},
                json={"model": hop["runtime_model"], "stream": stream, "messages": []},
            ) as response:
                assert response.status == 200, await response.text() if response.status != 200 else ""
                origin = json.loads(response.headers[SERVED_HOP_HEADER])
                assert origin == {"provider": "anthropic", "api": fallback_protocol, "model": "模型/β"}
                assert response.headers[SERVED_HOP_HEADER].isascii()
                assert await response.read() == answer
        completion = router.settle_turn(
            "turn-fallback", settled_by=SETTLED_BY_TERMINAL_RESULT, ts=NOW.isoformat(),
        )
        if completion:
            await completion
        record = service.provenance.get("turn-fallback")
        _validate("turn-provenance.schema.json", record)
        assert record["served"]["origin"] == origin
        assert record["served"]["source_id"] == second.id
        assert record["failed_attempts"][0]["source_id"] == first.id
        assert [entry[0] for entry in service.adapter.invocations] == [first.id, second.id]
        assert all(request.protocol == protocol for request in service.adapter.requests)
        assert all(SERVED_HOP_HEADER not in request.headers for request in service.adapter.requests)
    finally:
        await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["upstream_terminal", "local_spool"])
async def test_buffered_failure_retains_the_admitted_origin(tmp_path, monkeypatch, failure):
    """MH-AVIBE-002: terminal/delivery errors must not lose an already known producer.

    Existing success/stream cases never enter the buffered error-response paths.
    """
    source = _source("src_primary01", "Primary", vendor="anthropic", protocol="anthropic", model_id="模型/β")
    outcome = (
        _outcome(RawOutcomeKind.HTTP_ERROR, status=400, code="invalid_request_error", source_id=source.id)
        if failure == "upstream_terminal" else _outcome(RawOutcomeKind.SUCCESS, source_id=source.id)
    )
    service = _avibe_service(tmp_path, [source], handles=[LiveInvokeHandle(outcome, (b"{}",))])
    if failure == "local_spool":
        def unavailable_spool(*args, **kwargs):
            raise OSError("fixture spool unavailable")

        monkeypatch.setattr(
            "core.handlers.model_hub.turn_gateway.tempfile.SpooledTemporaryFile", unavailable_spool,
        )
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="avibe:test", turn_id="turn-buffered-failure")
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f'{hop["base_url"]}/messages', headers={"x-api-key": hop["token"]},
                json={"model": hop["runtime_model"], "stream": False, "messages": []},
            ) as response:
                assert response.status == (400 if failure == "upstream_terminal" else 502)
                origin = json.loads(response.headers[SERVED_HOP_HEADER])
                assert origin == {"provider": "anthropic", "api": "anthropic", "model": "模型/β"}
                assert (await response.json())["error"]
        completion = router.settle_turn(
            "turn-buffered-failure", settled_by=SETTLED_BY_TERMINAL_RESULT, ts=NOW.isoformat(),
        )
        if completion:
            await completion
        record = service.provenance.get("turn-buffered-failure")
        _validate("turn-provenance.schema.json", record)
        assert record["outcome"] == "failed_terminal"
        if failure == "upstream_terminal":
            assert record["terminal_error"]["origin"] == origin
        else:
            # Existing engine-down provenance deliberately does not blame the
            # upstream Source for local delivery failure, despite a known producer.
            assert record["terminal_error"]["reason"] == "engine_down"
            assert record["terminal_error"]["source_id"] is None
        assert [entry[0] for entry in service.adapter.invocations] == [source.id]
    finally:
        await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("model_id,allowed", [
    pytest.param("a" * 4044, True, id="exact-bound"),  # 52 bytes of JSON envelope.
    pytest.param("a" * 4045, False, id="one-over"),
    pytest.param("模" * 1350, False, id="unicode-expansion"),
])
async def test_origin_header_limit_is_checked_before_each_avibe_hop(tmp_path, model_id, allowed, fallback):
    """MH-AVIBE-002: transport bounds must reject a hop before any upstream work.

    Short-ID cases cannot expose client header parse failure or a fallback bypass.
    Persisted-shape acceptance is independent of the Avibe transport restriction.
    """
    target = _source("src_target001", "Target", protocol="openai_chat", model_id=model_id)
    sources = [target]
    handles = []
    if fallback:
        first = _source("src_primary01", "Primary", protocol="anthropic")
        sources.insert(0, first)
        handles.append(InvokeHandle(_outcome(
            RawOutcomeKind.HTTP_ERROR, status=429, code="rate_limit_error", source_id=first.id,
        )))
    handles.append(LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=target.id), (b"{}",)))
    service = _avibe_service(tmp_path, sources, handles=handles)
    reloaded = ModelHubConfig.from_payload(service.store.config.to_payload())
    assert reloaded.sources[-1].models[0].id == model_id
    assert reloaded.agents["avibe"].routes["menu-alias"].hops[-1].model_id == model_id
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="avibe:test", turn_id="turn-header-bound")
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f'{hop["base_url"]}/{PROTOCOL_ENDPOINTS[hop["protocol"]]}',
                headers={"Authorization": f'Bearer {hop["token"]}'},
                json={"model": hop["runtime_model"], "stream": False, "messages": []},
            ) as response:
                if allowed:
                    assert response.status == 200
                    assert len(response.headers[SERVED_HOP_HEADER].encode("ascii")) == 4096
                    assert json.loads(response.headers[SERVED_HOP_HEADER])["model"] == model_id
                    assert await response.read() == b"{}"
                else:
                    assert response.status == 422
                    assert SERVED_HOP_HEADER not in response.headers
                    assert (await response.json())["error"]["code"] == "served_hop_too_large"
        assert [entry[0] for entry in service.adapter.invocations] == (
            ([sources[0].id] if fallback else []) + ([target.id] if allowed else [])
        )
        assert target.state.status == "standby"
    finally:
        await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
async def test_served_hop_transport_bound_does_not_restrict_native_clients(tmp_path, backend):
    """Native consumers never emit this header, so their valid IDs remain callable."""
    source = _source("src_primary01", "Primary", model_id="模" * 1400)
    service = _service(tmp_path, sources=[source], outcomes=[_outcome(RawOutcomeKind.SUCCESS)])
    selected = _canonicalize_fixed_test_routes(service)
    resolved = await service.resolve(
        backend=backend, model_id=selected.get(backend, "shared-model"), request={}, stream=False,
    )
    assert resolved.origin is None
    assert resolved.model_id == source.models[0].id
    assert service.adapter.invocations == [(source.id, source.models[0].id, backend)]


@pytest.mark.asyncio
async def test_slow_anthropic_resolution_waits_to_publish_origin_and_keeps_source_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr("core.handlers.model_hub.turn_gateway._EARLY_STREAM_COMMIT_SECONDS", 0.001)
    source = _source("src_primary01", "Primary", vendor="anthropic", protocol="anthropic")
    answer = _body("anthropic", True)
    service = _avibe_service(tmp_path, [source], handles=[
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, stream_started=True), (answer,)),
    ])
    entered, release = asyncio.Event(), asyncio.Event()
    invoke = service.adapter.invoke

    async def held(*args, **kwargs):
        handle = await invoke(*args, **kwargs)
        entered.set()
        await release.wait()
        return handle

    service.adapter.invoke = held
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    task = None
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="avibe:test", turn_id="turn-slow")
        async with aiohttp.ClientSession() as client:
            task = asyncio.create_task(client.post(
                f'{hop["base_url"]}/messages', headers={"x-api-key": hop["token"]},
                json={"model": hop["runtime_model"], "stream": True, "messages": []},
            ))
            await asyncio.wait_for(entered.wait(), 2)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), 0.05)
            # Replace the persisted Source after admission: response metadata
            # must describe the actual invocation, not the latest config read.
            service.store.config.sources = [replace(source, vendor="custom", protocol="openai_chat")]
            release.set()
            response = await asyncio.wait_for(task, 2)
            async with response:
                origin = json.loads(response.headers[SERVED_HOP_HEADER])
                assert origin == HopOrigin("anthropic", "anthropic", "shared-model").payload()
                assert await response.read() == answer
        completion = router.settle_turn("turn-slow", settled_by=SETTLED_BY_TERMINAL_RESULT, ts=NOW.isoformat())
        if completion:
            await completion
        assert service.provenance.get("turn-slow")["served"]["origin"] == origin
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await gateway.close()


@pytest.mark.asyncio
async def test_concurrent_responses_keep_their_own_origin(tmp_path):
    """Interleaving requests must not borrow a gateway-wide 'last served' hop."""
    first = _source("src_primary01", "First", vendor="openai", protocol="openai_chat", model_id="model-one")
    second = _source("src_second001", "Second", vendor="anthropic", protocol="anthropic", model_id="model-two")
    service = _avibe_service(tmp_path, [first, second], handles=[
        LiveInvokeHandle(
            _outcome(RawOutcomeKind.SUCCESS, source_id=source.id, stream_started=True),
            (_body(source.protocol, True),),
        )
        for source in (first, second)
    ])
    agent = service.store.config.agents["avibe"]
    agent.models.append(ModelHubBackendModelConfig(id="second-alias"))
    agent.routes["menu-alias"] = ModelHubRouteConfig((ModelHubRouteHopConfig(first.id, "model-one"),))
    agent.routes["second-alias"] = ModelHubRouteConfig((ModelHubRouteHopConfig(second.id, "model-two"),))
    entered, release = asyncio.Event(), asyncio.Event()
    original_invoke = service.adapter.invoke

    async def invoke(*args, **kwargs):
        handle = await original_invoke(*args, **kwargs)
        if args[0] == first.id:
            entered.set()
            await release.wait()
        return handle

    service.adapter.invoke = invoke
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    pending = None
    try:
        hops = [
            await router.resolve_hop(alias, process_scope=f"avibe:session-{index}", turn_id=f"turn-{index}")
            for index, alias in enumerate(("menu-alias", "second-alias"))
        ]
        async with aiohttp.ClientSession() as client:
            async def read(hop):
                async with client.post(
                    f'{hop["base_url"]}/{PROTOCOL_ENDPOINTS[hop["protocol"]]}',
                    headers={"Authorization": f'Bearer {hop["token"]}'},
                    json={"model": hop["runtime_model"], "stream": True, "messages": []},
                ) as response:
                    assert response.status == 200
                    origin = json.loads(response.headers[SERVED_HOP_HEADER])
                    assert await response.read() == _body(hop["protocol"], True)
                    return origin

            pending = asyncio.create_task(read(hops[0]))
            await asyncio.wait_for(entered.wait(), 2)
            second_origin = await asyncio.wait_for(read(hops[1]), 2)
            release.set()
            first_origin = await asyncio.wait_for(pending, 2)
        assert first_origin == {"provider": "openai", "api": "openai_chat", "model": "model-one"}
        assert second_origin == {"provider": "anthropic", "api": "anthropic", "model": "model-two"}
        for index, origin in enumerate((first_origin, second_origin)):
            completion = router.settle_turn(f"turn-{index}", settled_by=SETTLED_BY_TERMINAL_RESULT, ts=NOW.isoformat())
            if completion:
                await completion
            assert service.provenance.get(f"turn-{index}")["served"]["origin"] == origin
    finally:
        release.set()
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await gateway.close()
