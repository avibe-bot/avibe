"""Codex launch, loopback routing and ownership share one request identity."""

from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest

from config.v2_config import (
    ModelHubBackendModelConfig,
    ModelHubRouteConfig,
    ModelHubRouteHopConfig,
)
from core.handlers.model_hub.adapter import RawOutcomeKind
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from core.run_settlement import SETTLED_BY_TERMINAL_RESULT
from modules.agents.codex.agent import CodexAgent
from modules.agents.model_hub import ModelHubRuntimeRouter, bind_launch, resolve_model_hub_launch
from tests.scenario_harness.model_hub import (
    MemoryModelHubStore,
    ModelHubScenarioAdapter,
    ScenarioCallResult,
    config_with_sources,
    service_for,
    source,
)


METADATA = "x-codex-turn-metadata"
ALIASES = ("研究模型", "review-model")
SUCCESS = ScenarioCallResult(
    RawOutcomeKind.SUCCESS, status=200,
    body=b'{"id":"resp_fixture","status":"completed","output":[]}',
)


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBE_MODEL_HUB_ENABLED", "1")
    sources = [
        source(f"src_request0{index}", ["same-upstream"], vendor="openai", protocol="openai_responses")
        for index in (1, 2)
    ]
    config = config_with_sources(sources, backend="codex")
    config.agents["codex"].models = [ModelHubBackendModelConfig(id=alias) for alias in ALIASES]
    config.agents["codex"].routes = {
        alias: ModelHubRouteConfig(hops=(ModelHubRouteHopConfig(supplied.id, "same-upstream"),))
        for alias, supplied in zip(ALIASES, sources, strict=True)
    }
    adapter = ModelHubScenarioAdapter(invoke_results=[SUCCESS] * 12)
    service = service_for(tmp_path, MemoryModelHubStore(config), adapter)
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    fixture = SimpleNamespace(
        gateway=gateway, router=router, adapter=adapter, service=service, cwd=str(tmp_path),
    )
    try:
        yield fixture
    finally:
        await gateway.close()


async def _launch(runtime, turn, model=ALIASES[0], *, scope=None):
    return await runtime.router.resolve(
        "codex", model, process_scope=scope or runtime.cwd, turn_id=turn,
    )


def _headers(launch):
    return {
        "Authorization": f"Bearer {launch.gateway_token}",
        METADATA: json.dumps(launch.gateway_request_metadata),
    }


def _body(launch):
    return {
        "model": launch.runtime_model,
        "input": "验证中文输入",
        "stream": False,
        "client_metadata": {
            "keep": "native-value",
            METADATA: json.dumps({
                "thread_id": "native-thread", **launch.gateway_request_metadata,
            }),
        },
    }


async def _post(launch, *, headers=None, payload=None):
    async with aiohttp.ClientSession(trust_env=False) as client:
        async with client.post(
            f"{launch.gateway_base_url}/v1/responses",
            headers=_headers(launch) if headers is None else headers,
            json=_body(launch) if payload is None else payload,
        ) as response:
            return response.status, await response.read()


async def _settle(runtime, turn):
    completion = runtime.router.settle_turn(
        turn, settled_by=SETTLED_BY_TERMINAL_RESULT, ts="2026-09-19T00:00:00Z",
    )
    if inspect.isawaitable(completion):
        await completion


async def test_codex_shared_transport_routes_overlapping_aliases(runtime):
    """MH-CODEX-ROUTING-001: two aliases share one process, not one request identity."""
    first, second = await asyncio.gather(
        _launch(runtime, "first"), _launch(runtime, "second", ALIASES[1]),
    )
    assert first.gateway_token == second.gateway_token
    assert first.fingerprint == second.fingerprint
    assert first.gateway_request_metadata != second.gateway_request_metadata
    cached = SimpleNamespace(is_initialized=True, runtime_fingerprint=first.fingerprint, stop=AsyncMock())
    agent = object.__new__(CodexAgent)
    agent._transports = {runtime.cwd: cached}
    agent._transport_locks = {}
    agent._transport_cwd_inodes = {}
    agent._transport_last_activity = {}
    agent._attach_transport_activation = Mock()
    agent._has_active_turns_for_cwd = Mock(return_value=True)
    agent._runtime_ownership_snapshot_for_cwd = Mock(
        return_value=SimpleNamespace(blocks_transport_replacement=True),
    )
    assert await asyncio.wait_for(agent._get_or_create_transport(runtime.cwd, second), 1) is cached
    cached.stop.assert_not_awaited()
    agent._has_active_turns_for_cwd.assert_not_called()
    agent._runtime_ownership_snapshot_for_cwd.assert_not_called()

    results = await asyncio.gather(_post(first), _post(second))
    assert [status for status, _ in results] == [200, 200]
    await _settle(runtime, "first")
    await _settle(runtime, "second")
    for turn, alias, source_id in (
        ("first", ALIASES[0], "src_request01"), ("second", ALIASES[1], "src_request02"),
    ):
        record = runtime.service.get_turn_provenance(turn)
        assert record["requested_model_id"] == alias
        assert record["served"]["source_id"] == source_id
        assert record["outcome"] == "served"
    for request in runtime.adapter.requests:
        assert request["model"] in ALIASES
        assert request["client_metadata"]["keep"] == "native-value"
        assert json.loads(request["client_metadata"][METADATA]) == {"thread_id": "native-thread"}
        assert METADATA not in request.headers
    assert {model for _source, model, _origin in runtime.adapter.invocations} == {"same-upstream"}


async def test_codex_untracked_launch_routes_without_claiming_live_turn(runtime):
    """MH-CODEX-ROUTING-001: IM/CLI launches need a route, not an invented FSM turn."""
    tracked = await _launch(runtime, "tracked")
    controller = SimpleNamespace(
        model_hub_runtime=runtime.router,
        session_turns=SimpleNamespace(model_hub_turn_id_for_task=Mock(return_value=None)),
    )
    untracked = await resolve_model_hub_launch(
        controller, "codex", ALIASES[1], process_scope=runtime.cwd,
    )
    assert untracked.gateway_token == tracked.gateway_token
    assert untracked.fingerprint == tracked.fingerprint
    assert untracked.gateway_request_metadata["avibe_turn_id"] == ""
    assert untracked.gateway_request_metadata["avibe_route_id"]
    runtime.adapter.invoke_results.clear()
    runtime.adapter.invoke_results.extend([
        ScenarioCallResult(RawOutcomeKind.HTTP_ERROR, status=404, error_code="model_not_found"),
        SUCCESS,
    ])
    assert (await _post(untracked))[0] == 400
    assert (await _post(tracked))[0] == 200
    await _settle(runtime, "tracked")
    record = runtime.service.get_turn_provenance("tracked")
    assert record["outcome"] == "served"
    assert record["failed_attempts"] == []
    assert record["terminal_error"] is None
    assert [source_id for source_id, _model, _origin in runtime.adapter.invocations] == [
        "src_request02", "src_request01",
    ]
    assert not runtime.gateway.correlation._traces
    assert not runtime.gateway.correlation._scopes[("codex", runtime.cwd)].active_turns


@pytest.mark.parametrize("bad_identity", [
    "missing", "missing-turn", "whitespace-turn", "malformed", "unknown", "unicode-route", "foreign",
    "mismatched-body", "wrong-model", "wrong-live-turn",
])
async def test_codex_invalid_identity_cannot_route_or_poison_peer(runtime, bad_identity):
    launch = await _launch(runtime, "healthy")
    headers = _headers(launch)
    payload = _body(launch)
    if bad_identity == "missing":
        headers.pop(METADATA)
    elif bad_identity in {"missing-turn", "whitespace-turn"}:
        metadata = dict(launch.gateway_request_metadata)
        if bad_identity == "missing-turn":
            metadata.pop("avibe_turn_id")
        else:
            metadata["avibe_turn_id"] = " "
        headers[METADATA] = json.dumps(metadata)
        payload.pop("client_metadata")
    elif bad_identity == "malformed":
        headers[METADATA] = "{"
    elif bad_identity in {"unknown", "unicode-route"}:
        headers[METADATA] = json.dumps({
            "avibe_route_id": "未知路由" if bad_identity == "unicode-route" else "unknown",
            "avibe_turn_id": "healthy",
        })
        payload.pop("client_metadata")
    elif bad_identity == "foreign":
        foreign = await _launch(runtime, "foreign", scope="another-process")
        headers[METADATA] = json.dumps(foreign.gateway_request_metadata)
        payload = _body(foreign)
    elif bad_identity == "mismatched-body":
        payload["client_metadata"][METADATA] = json.dumps({
            **launch.gateway_request_metadata, "avibe_turn_id": "some-peer",
        })
    elif bad_identity == "wrong-model":
        payload["model"] = "never-launched"
    else:
        await _launch(runtime, "peer", ALIASES[1])
        headers[METADATA] = json.dumps({
            **launch.gateway_request_metadata, "avibe_turn_id": "peer",
        })
        payload.pop("client_metadata")
    status, _ = await _post(launch, headers=headers, payload=payload)
    assert status in {400, 409}
    assert runtime.adapter.invocations == []
    assert (await _post(launch))[0] == 200
    await _settle(runtime, "healthy")
    record = runtime.service.get_turn_provenance("healthy")
    assert record["outcome"] == "served"
    assert record["served"]["source_id"] == "src_request01"


async def test_codex_late_request_same_route_is_not_owned_by_successor(runtime):
    old = await _launch(runtime, "old")
    await _settle(runtime, "old")
    current = await _launch(runtime, "current")
    assert old.gateway_request_metadata["avibe_route_id"] == current.gateway_request_metadata["avibe_route_id"]
    runtime.adapter.invoke_results.clear()
    runtime.adapter.invoke_results.extend([
        ScenarioCallResult(RawOutcomeKind.HTTP_ERROR, status=404, error_code="model_not_found"),
        SUCCESS,
    ])
    assert (await _post(old))[0] == 400
    assert (await _post(current))[0] == 200
    await _settle(runtime, "current")
    record = runtime.service.get_turn_provenance("current")
    assert record["outcome"] == "served"
    assert record["failed_attempts"] == []
    assert record["terminal_error"] is None
    assert runtime.service.provenance.get("old") is None


@pytest.mark.parametrize("malformed", [False, True])
async def test_codex_request_ownership_precedes_body_parse(runtime, malformed):
    launch = await _launch(runtime, "parsing")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def body(**_kwargs):
        entered.set()
        await release.wait()
        return {} if malformed else _body(launch)

    request = SimpleNamespace(
        headers=_headers(launch), match_info={"backend": "codex", "endpoint": "responses"},
        json=body,
    )
    running = asyncio.create_task(runtime.gateway._handle_request(request))
    completion = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        completion = asyncio.create_task(_settle(runtime, "parsing"))
        await asyncio.sleep(0)
        assert not completion.done()
        release.set()
        response = await asyncio.wait_for(running, 2)
        await asyncio.wait_for(completion, 2)
        assert response.status == (400 if malformed else 200)
        record = runtime.service.get_turn_provenance("parsing")
        assert record["outcome"] == ("failed_terminal" if malformed else "served")
    finally:
        release.set()
        await asyncio.gather(*(task for task in (running, completion) if task), return_exceptions=True)


async def test_codex_adapter_keeps_request_metadata_on_compatibility_retry(runtime):
    launch = await _launch(runtime, "adapter")
    context = SimpleNamespace(platform_specific={})
    bind_launch(context, launch)
    agent = object.__new__(CodexAgent)
    agent.controller = SimpleNamespace()
    agent._prompt_state_agent_session_id = Mock(return_value="session")
    agent._resolve_codex_agent_settings = Mock(return_value=(None, launch.runtime_model, None, None))
    agent._build_input = Mock(return_value=[{"type": "text", "text": "中文"}])
    agent._write_caller_env_script = Mock()
    agent._turn_registry = SimpleNamespace(
        begin_turn_start=Mock(), get_bootstrapped_turn_id=Mock(return_value=None),
        finalize_turn_start_response=Mock(return_value=SimpleNamespace()),
    )
    request = SimpleNamespace(
        context=context, base_session_id="session", session_key="session",
        composite_session_id="session", vibe_agent_model_explicit=True,
    )
    agent._thread_prompt_strategies = {"session": ("native-thread", "collaboration")}
    transport = SimpleNamespace(
        supports_turn_collaboration_mode=True,
        send_request=AsyncMock(side_effect=[
            RuntimeError("unknown field collaborationMode: experimental API unsupported"),
            {"turn": {"id": "native-turn"}},
        ]),
    )
    await agent._start_turn(transport, request, "native-thread")
    calls = transport.send_request.await_args_list
    assert len(calls) == 2
    for call in calls:
        assert call.args[0] == "turn/start"
        assert call.args[1]["responsesapiClientMetadata"] == launch.gateway_request_metadata
        assert call.args[1]["model"] == launch.runtime_model
