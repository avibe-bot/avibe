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
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from jsonschema import Draft7Validator, ValidationError
from referencing import Registry, Resource

from config.v2_config import (
    ModelHubAgentSupplyConfig,
    ModelHubBackendModelConfig,
    ModelHubConfig,
    ModelHubModelConfig,
    ModelHubRouteConfig,
    ModelHubRouteHopConfig,
    ModelHubSourceConfig,
    ModelHubSourceStateConfig,
)
from core.handlers.model_hub.adapter import RawOutcomeKind
from core.handlers.model_hub.provenance import BoundedProvenanceStore, HopOrigin, SERVED_HOP_HEADER
from core.handlers.model_hub.request import ModelHubRequest
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
# Consumer promises, deliberately independent of the producer schema. Positive
# payload validation and the channel matrix cannot detect a missing requirement.
MANDATORY_FIELDS = {
    "hop-resolution.schema.json": {
        (): (
            "backend", "requested_model", "protocol", "base_url", "token",
            "runtime_model", "source_id", "provider", "request_headers", "capabilities",
        ),
        ("capabilities",): (
            "context_window", "input_limit", "max_output_tokens", "supports_tools",
            "supports_images", "supports_reasoning", "reasoning_efforts",
        ),
    },
    "turn-provenance.schema.json": {
        (): (
            "contract_version", "turn_id", "ts", "agent", "requested_model_id",
            "outcome", "failed_attempts", "served", "terminal_error", "canceled_attempt",
            "model_supply_state", "blockers",
        ),
        ("served",): ("source_id", "configured_model_id", "channel", "origin"),
        ("served", "origin"): ("provider", "api", "model"),
    },
}


def _validate(schema_name, value):
    resources = {}
    for path in CONTRACTS.glob("*.schema.json"):
        payload = json.loads(path.read_text())
        resources[payload.get("$id", path.name)] = Resource.from_contents(payload)
    schema = json.loads((CONTRACTS / schema_name).read_text())
    Draft7Validator(schema, registry=Registry().with_resources(resources.items())).validate(value)


def _assert_mandatory_fields(schema_name, payload):
    for path, fields in MANDATORY_FIELDS[schema_name].items():
        for field in fields:
            incomplete = copy.deepcopy(payload)
            target = incomplete
            for key in path:
                target = target[key]
            target.pop(field)
            with pytest.raises(ValidationError) as missing:
                _validate(schema_name, incomplete)
            assert missing.value.validator == "required"
            assert list(missing.value.absolute_path) == list(path)
            assert missing.value.message == f"'{field}' is a required property"


def _vibey_service(tmp_path, sources, *, handles=()):
    service = _service(tmp_path, sources=sources, live_handles=list(handles))
    _canonicalize_fixed_test_routes(service)
    agent = service.store.config.agents["vibey"]
    agent.models = [ModelHubBackendModelConfig(id="menu-alias")]
    agent.sources.order = [source.id for source in sources if source.supply_channel == "hub"]
    agent.routes["menu-alias"] = ModelHubRouteConfig(tuple(
        ModelHubRouteHopConfig(source.id, source.models[0].id)
        for source in sources if source.supply_channel == "hub"
    ))
    service.store.requested_models["vibey"] = "menu-alias"
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


def _signed_history(protocol):
    """Wire-shaped histories and their explicit, non-opaque equivalents."""
    if protocol == "anthropic":
        call = {"type": "tool_use", "id": "call_1", "name": "verify", "input": {"signature": "user-data"}}
        result = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "ok"}]}
        return (
            {"messages": [{"role": "assistant", "content": [
                {"type": "thinking", "thinking": "Visible reasoning", "signature": "opaque-thinking"},
                {"type": "redacted_thinking", "data": "opaque-redacted"},
                {"type": "text", "text": "Answer", "signature": "opaque-text"},
                {**call, "thoughtSignature": "opaque-call"},
            ]}, result, {"role": "assistant", "content": [{"type": "redacted_thinking", "data": "opaque-only"}]}]},
            {"messages": [{"role": "assistant", "content": [
                {"type": "text", "text": "Visible reasoning"}, {"type": "text", "text": "Answer"}, call,
            ]}, result]},
        )
    if protocol == "openai_responses":
        call = {"type": "function_call", "call_id": "call_1", "name": "verify", "arguments": '{"signature":"user-data"}'}
        result = {"type": "function_call_output", "call_id": "call_1", "output": "ok"}
        return (
            {"input": [
                {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque-reasoning",
                 "summary": [{"type": "summary_text", "text": "Visible reasoning"}]},
                {"type": "reasoning", "id": "rs_hidden", "encrypted_content": "opaque-only", "summary": []},
                {"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "Answer", "signature": "opaque-text"},
                ]},
                {**call, "signature": "opaque-call"}, result,
            ], "store": False},
            {"input": [
                {"role": "assistant", "content": [{"type": "output_text", "text": "Visible reasoning"}]},
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Answer"}]},
                call, result,
            ], "store": False},
        )
    call = {"type": "function", "id": "call_1", "function": {"name": "verify", "arguments": '{"signature":"user-data"}'}}
    result = {"role": "tool", "tool_call_id": "call_1", "content": "ok"}
    return (
        {"messages": [{"role": "assistant",
                       "content": [{"type": "text", "text": "Answer", "thoughtSignature": "opaque-text"}],
                       "tool_calls": [{**call, "extra_content": {"google": {"thought_signature": "opaque-call"}}}]},
                      result]},
        {"messages": [{"role": "assistant", "content": [{"type": "text", "text": "Answer"}],
                       "tool_calls": [{**call, "extra_content": {"google": {}}}]}, result]},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", PROTOCOL_ENDPOINTS)
@pytest.mark.parametrize("change", ["same_origin", "provider", "api", "model"])
async def test_failover_strips_opaque_history_only_when_origin_changes(tmp_path, protocol, change):
    """MH-VIBEY-005: the engine must never receive primary-signed history at another origin.

    Origin-report tests alone do not inspect the outgoing history at admission.
    Source identity alone is not origin identity; preserve ordinary tool data.
    """
    primary = _source("src_primary01", "Primary", vendor="anthropic", protocol=protocol)
    fallback_protocol = "anthropic" if protocol != "anthropic" else "openai_responses"
    fallback = _source(
        "src_fallback01", "Fallback",
        vendor="custom" if change == "provider" else primary.vendor,
        protocol=fallback_protocol if change == "api" else protocol,
        model_id="another-model" if change == "model" else "shared-model",
    )
    service = _vibey_service(tmp_path, [primary, fallback], handles=[
        InvokeHandle(_outcome(RawOutcomeKind.HTTP_ERROR, status=429, code="rate_limit_error")),
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=fallback.id), (b"{}",)),
    ])
    signed, plain = _signed_history(protocol)
    original = copy.deepcopy(signed)
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="vibey:opaque", turn_id="turn-opaque")
        common = {"model": hop["runtime_model"], "stream": False, "temperature": 0.25}
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f'{hop["base_url"]}/{PROTOCOL_ENDPOINTS[protocol]}',
                headers={
                    "Authorization": f'Bearer {hop["token"]}',
                    "anthropic-beta": "fixture-beta",
                    "User-Agent": "fixture-agent",
                },
                json={**common, **signed},
            ) as response:
                assert response.status == 200
                assert await response.read() == b"{}"
                assert json.loads(response.headers[SERVED_HOP_HEADER]) == {
                    "provider": fallback.vendor, "api": fallback.protocol, "model": fallback.models[0].id,
                }
        assert [entry[0] for entry in service.adapter.invocations] == [primary.id, fallback.id]
        assert service.adapter.requests[0] == {**common, **original}
        assert service.adapter.requests[1] == {**common, **(original if change == "same_origin" else plain)}
        assert all(request.protocol == protocol for request in service.adapter.requests)
        assert all(request.headers == {
            "anthropic-beta": "fixture-beta", "user-agent": "fixture-agent",
        } for request in service.adapter.requests)
        assert signed == original
    finally:
        await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("turn_id", ["turn-snapshot", None])
@pytest.mark.parametrize("origin_state", ["unchanged", "changed", "unverified"])
async def test_opaque_history_uses_launch_snapshot_without_requiring_turn_attribution(tmp_path, turn_id, origin_state):
    """MH-VIBEY-005: config reload and untracked calls cannot invent primary provenance.

    A fallback-only case cannot catch snapshot recomputation at HTTP arrival or
    loss of the route credential when no dispatched turn is being tracked.
    """
    primary = _source("src_primary01", "Primary", vendor="anthropic", protocol="anthropic")
    service = _vibey_service(tmp_path, [primary], handles=[
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS), (b"{}",)),
    ])
    signed, plain = _signed_history("anthropic")
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    try:
        if origin_state == "unverified":
            base, token = await gateway.endpoint("vibey", process_scope="vibey:unverified")
            base_url, model = f"{base}/v1", "menu-alias"
        else:
            hop = await router.resolve_hop("menu-alias", process_scope="vibey:snapshot", turn_id=turn_id)
            base_url, model, token = hop["base_url"], hop["runtime_model"], hop["token"]
        if origin_state == "changed":
            service.store.config.sources[0].vendor = "custom"
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{base_url}/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={"model": model, **signed},
            ) as response:
                assert response.status == 200
                assert await response.read() == b"{}"
        assert service.adapter.requests[0]["messages"] == (
            signed if origin_state == "unchanged" else plain
        )["messages"]
    finally:
        await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("recovered_hop", ["primary", "fallback"])
async def test_opaque_history_origin_survives_recovery_walks(tmp_path, recovered_hop):
    """MH-VIBEY-005: retries keep the launch origin and never mutate signed input.

    Single-walk failover misses both a fresh resolver walk trusting its new first
    candidate and a destructive scrub leaking back into same-origin recovery.
    """
    primary = _source("src_primary01", "Primary", vendor="anthropic", protocol="anthropic")
    fallback = _source("src_fallback01", "Fallback", vendor="custom", protocol="anthropic")
    service = _vibey_service(tmp_path, [primary, fallback], handles=[
        InvokeHandle(_outcome(RawOutcomeKind.HTTP_ERROR, status=503, source_id=primary.id)),
        InvokeHandle(_outcome(RawOutcomeKind.HTTP_ERROR, status=503, source_id=fallback.id)),
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS), (b"{}",)),
    ])
    service.recovery.window_seconds = 120
    clock = {"now": NOW}
    service.now = lambda: clock["now"]
    waits = []

    async def advance(delay):
        waits.append(delay)
        clock["now"] += timedelta(seconds=delay)
        if recovered_hop == "fallback":
            service.store.config.agents["vibey"].routes["menu-alias"] = ModelHubRouteConfig((
                ModelHubRouteHopConfig(fallback.id, "shared-model"),
            ))

    service.recovery.sleep = advance
    signed, plain = _signed_history("anthropic")
    gateway = ModelHubTurnGateway(service, now=lambda: clock["now"])
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="vibey:recovery", turn_id="turn-recovery")
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f'{hop["base_url"]}/messages',
                headers={"Authorization": f'Bearer {hop["token"]}'},
                json={"model": hop["runtime_model"], **signed},
            ) as response:
                assert response.status == 200
                assert await response.read() == b"{}"
        assert waits == [30.0]
        assert [entry[0] for entry in service.adapter.invocations] == [
            primary.id, fallback.id, primary.id if recovered_hop == "primary" else fallback.id,
        ]
        assert [request["messages"] for request in service.adapter.requests] == [
            signed["messages"], plain["messages"],
            (signed if recovered_hop == "primary" else plain)["messages"],
        ]
    finally:
        await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
async def test_native_history_is_not_scrubbed_by_vibey_origin_policy(tmp_path, backend):
    """Native callers have no primary-origin metadata; their existing policy stays intact."""
    protocol = {"claude": "anthropic", "codex": "openai_responses", "opencode": "openai_chat"}[backend]
    primary = _source("src_primary01", "Primary", protocol=protocol)
    fallback = _source("src_fallback01", "Fallback", vendor="custom", protocol=protocol)
    service = _service(tmp_path, sources=[primary, fallback], outcomes=[
        _outcome(RawOutcomeKind.HTTP_ERROR, status=429),
        _outcome(RawOutcomeKind.SUCCESS),
    ])
    selected = _canonicalize_fixed_test_routes(service)
    signed, _plain = _signed_history(protocol)
    original = copy.deepcopy(signed)
    await service.resolve(
        backend=backend, model_id=selected.get(backend, "shared-model"),
        request=ModelHubRequest(signed, protocol=protocol), stream=False,
    )
    assert [entry[0] for entry in service.adapter.invocations] == [primary.id, fallback.id]
    assert service.adapter.requests == [original, original]


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", ["unknown", "source", "catalog"])
async def test_vibey_candidate_capabilities_survive_catalog_admission_without_guesses(tmp_path, metadata):
    """MH-VIBEY-001: candidate -> persisted catalog -> launch preserves authority.

    Hand-populated launch fixtures never exercise the candidate model-family
    default, which must not invent a reasoning ladder for a relay model.
    """
    source = _source("src_primary01", "Primary", protocol="openai_chat", model_id="claude-unknown")
    if metadata == "source":
        source.models[0].reasoning_efforts = ["high"]
    service = _vibey_service(tmp_path, [source])
    service.store.config.agents["vibey"].models = []
    service.store.config.agents["vibey"].routes = {}
    service.models_dev_catalog = lambda: (
        {"fixture": {"models": {"claude-unknown": {
            "reasoning": True, "reasoning_options": [{"type": "effort", "values": ["medium"]}],
        }}}}
        if metadata == "catalog" else {}
    )
    candidate, = service.agent_model_candidates("vibey")["providers"]
    desired = {key: value for key, value in candidate.items() if key != "suppliers"}
    await service.set_agent_models("vibey", [], [desired])
    gateway = ModelHubTurnGateway(service)
    try:
        hop = await ModelHubRuntimeRouter(service=service, turn_gateway=gateway).resolve_hop(
            "claude-unknown", process_scope="vibey:candidate", turn_id="turn-candidate",
        )
        assert hop["protocol"] == "openai_chat"
        assert hop["capabilities"]["reasoning_efforts"] == {
            "unknown": [], "source": ["high"], "catalog": ["medium"],
        }[metadata]
        assert hop["capabilities"]["supports_reasoning"] is (True if metadata == "catalog" else None)
        assert hop["capabilities"]["input_limit"] is None
    finally:
        await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("late_request", ["malformed", "valid"])
async def test_sequential_vibey_retry_replaces_route_and_retains_served_provenance(tmp_path, late_request):
    """MH-VIBEY-006: a retryable partial response can resolve a new hop in one turn.

    One-request failover never re-prepares a route, so it misses the native
    one-launch assumption marking an otherwise successful Avibe turn ambiguous.
    """
    primary = _source("src_primary01", "Primary", vendor="anthropic", protocol="anthropic")
    fallback = _source("src_fallback01", "Fallback", model_id="next-model")
    late = _source("src_late00001", "Late continuation", model_id="late-model")
    partial = b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"partial"}}\n\n'
    service = _vibey_service(tmp_path, [primary, fallback, late], handles=[
        LiveInvokeHandle(_outcome(
            RawOutcomeKind.HTTP_ERROR, status=429, code="rate_limit_error",
            source_id=primary.id, stream_started=True,
        ), (partial,)),
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=fallback.id), (b"{}",)),
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=late.id), (b"{}",)),
    ])
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    turn_id = "turn-sequential"
    try:
        initial = await router.resolve_hop("menu-alias", process_scope="vibey:retry", turn_id=turn_id)
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f'{initial["base_url"]}/messages',
                headers={"Authorization": f'Bearer {initial["token"]}'},
                json={"model": initial["runtime_model"], "stream": True, "messages": []},
            ) as response:
                assert response.status == 200
                assert b"partial" in await response.read()
            retry = await router.resolve_hop("menu-alias", process_scope="vibey:retry", turn_id=turn_id)
            assert retry["source_id"] == fallback.id
            async with client.post(
                f'{retry["base_url"]}/responses',
                headers={"Authorization": f'Bearer {retry["token"]}'},
                json={"model": retry["runtime_model"], "input": "retry"},
            ) as response:
                assert response.status == 200
                await response.read()
                origin = json.loads(response.headers[SERVED_HOP_HEADER])
            # A late old credential must not arm/poison the retry's attribution,
            # even if the request is invalid before the model is parsed.
            service.store.config.agents["vibey"].routes["menu-alias"] = ModelHubRouteConfig((
                ModelHubRouteHopConfig(late.id, "late-model"),
            ))
            async with client.post(
                f'{initial["base_url"]}/messages',
                headers={"Authorization": f'Bearer {initial["token"]}'},
                **(
                    {"data": "invalid json"} if late_request == "malformed"
                    else {"json": {"model": initial["runtime_model"], "messages": []}}
                ),
            ) as response:
                assert response.status == (400 if late_request == "malformed" else 200)
                await response.read()
                if late_request == "valid":
                    assert json.loads(response.headers[SERVED_HOP_HEADER])["model"] == "late-model"
        completion = router.settle_turn(turn_id, settled_by=SETTLED_BY_TERMINAL_RESULT, ts=NOW.isoformat())
        if completion:
            await completion
        record = service.provenance.get(turn_id)
        assert record is not None
        assert record["outcome"] == "served"
        assert record["served"]["source_id"] == fallback.id
        assert record["served"]["origin"] == origin
        _validate("turn-provenance.schema.json", record)
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_vibey_route_replacement_refuses_overlap_without_poisoning_current_request(tmp_path):
    """MH-VIBEY-006: a new launch cannot take an in-flight request's turn identity."""
    primary = _source("src_primary01", "Primary", vendor="anthropic", protocol="anthropic")
    fallback = _source("src_fallback01", "Fallback", protocol="anthropic", model_id="next-model")
    service = _vibey_service(tmp_path, [primary, fallback], handles=[
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=primary.id), (b"{}",)),
    ])
    entered, release = asyncio.Event(), asyncio.Event()
    invoke = service.adapter.invoke

    async def held(*args, **kwargs):
        result = await invoke(*args, **kwargs)
        entered.set()
        await release.wait()
        return result

    service.adapter.invoke = held
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    turn_id = "turn-overlap"
    try:
        first = await router.resolve_hop("menu-alias", process_scope="vibey:overlap", turn_id=turn_id)
        async with aiohttp.ClientSession() as client:
            pending = asyncio.create_task(client.post(
                f'{first["base_url"]}/messages',
                headers={"Authorization": f'Bearer {first["token"]}'},
                json={"model": first["runtime_model"], "messages": []},
            ))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                service.store.config.agents["vibey"].routes["menu-alias"] = ModelHubRouteConfig((
                    ModelHubRouteHopConfig(fallback.id, "next-model"),
                ))
                with pytest.raises(ModelHubError) as conflict:
                    await router.resolve_hop("menu-alias", process_scope="vibey:overlap", turn_id=turn_id)
                assert conflict.value.status == 409
            finally:
                release.set()
                response = await pending
                assert response.status == 200
                await response.read()
                response.release()
        completion = router.settle_turn(turn_id, settled_by=SETTLED_BY_TERMINAL_RESULT, ts=NOW.isoformat())
        if completion:
            await completion
        record = service.provenance.get(turn_id)
        assert record is not None
        assert record["served"]["source_id"] == primary.id
    finally:
        await gateway.close()


def test_released_config_adds_only_empty_vibey_supply():
    """MH-VIBEY-003: preserve shipped native rows and add an empty Hub consumer."""
    released = json.loads((FIXTURES / "released_pre_vibey_consumer.json").read_text())
    before = copy.deepcopy(released)
    config = ModelHubConfig.from_payload(released)
    assert released == before
    assert set(config.agents) == {"claude", "codex", "opencode", "vibey"}
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
    vibey = config.agents["vibey"]
    assert (vibey.mode, vibey.menu_kind, vibey.models, vibey.sources.order, vibey.routes) == (
        "hub", "fixed", [], [], {},
    )
    assert config.enabled is before["enabled"]
    assert ModelHubConfig.from_payload(config.to_payload()).to_payload() == config.to_payload()


class _PersistedStore:
    """Reads and writes the persisted payload, as the config file does."""

    def __init__(self, payload: dict):
        self.payload = payload

    def load(self) -> ModelHubConfig:
        return ModelHubConfig.from_payload(copy.deepcopy(self.payload))

    def save(self, config: ModelHubConfig) -> None:
        self.payload = config.to_payload()


def _seed_source(source_id, day, *model_ids, kind="api_key", vendor="openai", protocol="openai_responses", channel="hub"):
    return ModelHubSourceConfig(
        id=source_id, kind=kind, vendor=vendor, display_name=source_id[4:], protocol=protocol,
        supply_channel=channel, billing="metered" if kind == "api_key" else "monthly",
        state=ModelHubSourceStateConfig(status="active" if channel == "native_cli" else "standby"),
        models=[ModelHubModelConfig(id=model_id, provenance="discovered") for model_id in model_ids],
        created_at=f"2026-01-0{day}T00:00:00Z",
        credential_ref=None if channel == "native_cli" else f"cred_{source_id}",
    )


SEED_ANTHROPIC = _seed_source(
    "src_seedanthropic", 1, "claude-opus-5-5", "claude-sonnet-5-5", vendor="anthropic", protocol="anthropic",
)
SEED_RESPONSES = _seed_source("src_seedresponses", 2, "gpt-5.6-sol")
SEED_CHAT = _seed_source("src_seedchat0001", 3, "模型/β", vendor="custom", protocol="openai_chat")
SEED_SUBSCRIPTION = _seed_source("src_seedsubscribe", 4, "gpt-5.6-sol", kind="subscription")
SEED_NATIVE = _seed_source(
    "src_seednative01", 5, "claude-opus-5-5",
    kind="subscription", vendor="anthropic", protocol="anthropic", channel="native_cli",
)
SEED_KEYS = [SEED_ANTHROPIC.id, SEED_RESPONSES.id, SEED_CHAT.id]
SEED_CATALOG = {"anthropic": {"name": "Anthropic", "models": {
    "claude-opus-5-5": {"name": "Claude Opus 5.5", "limit": {"context": 1_000_000, "output": 128_000}},
}}}


def _seed_service(tmp_path, payload, selections):
    """A service over persisted state, with the controller's two Agent-row hooks."""
    service = _service(tmp_path, sources=[])
    service.store = _PersistedStore(payload)
    service.models_dev_catalog = lambda: SEED_CATALOG
    service.builtin_agent_models_override = lambda: list(selections)
    announced = []

    async def catalog_changed(backend):
        announced.append(backend)

    service.backend_catalog_changed = catalog_changed
    return service, service.store, announced


@pytest.mark.asyncio
@pytest.mark.parametrize(("selections", "seeded"), [
    # Each built-in Agent's model, in order; OpenCode's names its provider, and
    # a model reached twice is listed once.
    (
        [("claude", "claude-opus-5-5"), ("codex", "gpt-5.6-sol"), ("opencode", "openai/gpt-5.6-sol")],
        ["claude-opus-5-5", "gpt-5.6-sol"],
    ),
    # A model no Source lists is no start, and a menu id may carry a slash.
    (
        [("claude", "opus"), ("codex", "gpt-6-astra"), ("opencode", "custom/模型/β")],
        ["模型/β"],
    ),
    # Nothing qualifies: the list starts empty, and the picker offers every
    # provider model.
    ([], []),
])
async def test_vibey_agent_predating_its_supply_starts_with_the_users_providers_and_models(
    tmp_path, selections, seeded,
):
    """MH-VIBEY-007: one seed from the Sources and Agent models the user already has.

    A config written before the Avibe Agent existed has Sources that were never
    placed for it, so its picker offered no provider model at all.
    """
    agents = {backend: ModelHubAgentSupplyConfig.default(backend, mode="hub") for backend in ("claude", "codex", "opencode")}
    # The user keeps one Source for Claude; seeding Avibe must not restore the rest.
    agents["claude"].sources.order = [SEED_ANTHROPIC.id]
    sources = [SEED_ANTHROPIC, SEED_RESPONSES, SEED_CHAT, SEED_SUBSCRIPTION, SEED_NATIVE]
    service, store, _announced = _seed_service(tmp_path, {
        "enabled": True,
        "runtime_default_applied": True,
        "sources": [source.to_payload() for source in sources],
        "agents": {backend: agent.to_payload() for backend, agent in agents.items()},
    }, selections)
    # An unrelated write ahead of the seed (a startup reconcile, a settings
    # save) must not record the empty placeholder as the user's entry.
    store.save(store.load())
    assert "vibey" not in store.payload["agents"]

    async def engine_not_started(_bindings):
        # Startup seeds before the engine is up; the seed must not depend on it.
        raise ModelHubError("engine_down", status=503)

    service.adapter.sync_sources = engine_not_started
    others = {backend: store.payload["agents"][backend] for backend in agents}

    assert await service.seed_vibey_supply() == seeded
    vibey = store.payload["agents"]["vibey"]
    # Every Source Avibe may use joins, subscriptions ahead of keys, whether or
    # not a starting model matches: like OpenCode, it reaches every vendor.
    assert vibey["sources"]["order"] == [SEED_SUBSCRIPTION.id, *SEED_KEYS]
    assert [model["id"] for model in vibey["models"]] == seeded
    assert {backend: store.payload["agents"][backend] for backend in agents} == others
    if "claude-opus-5-5" in seeded:
        row = next(model for model in vibey["models"] if model["id"] == "claude-opus-5-5")
        assert (row["origin"], row["display_name"], row["context_window"], row["max_output_tokens"]) == (
            "provider", "Claude Opus 5.5", 1_000_000, 128_000,
        )
    candidates = service.agent_model_candidates("vibey")
    listed = {model.id for source in (SEED_ANTHROPIC, SEED_RESPONSES, SEED_CHAT, SEED_SUBSCRIPTION) for model in source.models}
    assert {candidate["id"] for candidate in candidates["providers"]} == listed - set(seeded)
    assert [candidate["id"] for candidate in candidates["in_list"]] == seeded

    # The persisted entry is the user's: a Source they removed stays removed.
    vibey["sources"]["order"].remove(SEED_CHAT.id)
    kept = copy.deepcopy(store.payload)
    assert await service.seed_vibey_supply() == []
    assert store.payload == kept


@pytest.mark.asyncio
@pytest.mark.parametrize(("first", "selections", "seeded"), [
    (SEED_ANTHROPIC, [("claude", "claude-opus-5-5"), ("codex", "gpt-6-astra")], ["claude-opus-5-5"]),
    # Typical of a fresh install: no Agent runs a model this Source lists, and
    # none is picked for the user.
    (SEED_ANTHROPIC, [("codex", "gpt-6-astra")], []),
    # A subscription joins even when no starting model matches it, so its
    # catalog still reaches the picker.
    (SEED_SUBSCRIPTION, [("codex", "gpt-6-astra")], []),
])
async def test_fresh_install_seeds_vibey_with_its_first_source(tmp_path, first, selections, seeded):
    """MH-VIBEY-007: on a fresh install the first eligible Source seeds Avibe at once.

    A seed with no Source to place is not a seed: counted as done, it would
    leave every later Source without starting models, and skipping the pending
    entry at placement would leave the first Source out until a restart.
    """
    service, store, announced = _seed_service(tmp_path, ModelHubConfig().to_payload(), selections)
    assert "vibey" not in store.payload["agents"]

    assert await service.seed_vibey_supply() == []
    native = copy.deepcopy(SEED_NATIVE)
    await service._commit_new_source_locked(native)
    assert "vibey" not in store.payload["agents"]
    assert announced == []

    await service._commit_new_source_locked(copy.deepcopy(first))
    vibey = store.payload["agents"]["vibey"]
    assert vibey["sources"]["order"] == [first.id]
    assert [model["id"] for model in vibey["models"]] == seeded
    assert announced == ["vibey"]
    assert {candidate["id"] for candidate in service.agent_model_candidates("vibey")["providers"]} == (
        {model.id for model in first.models} - set(seeded)
    )

    # Seeded once: a later Source is placed like any other, and nothing re-seeds.
    await service._commit_new_source_locked(copy.deepcopy(SEED_CHAT))
    vibey = store.payload["agents"]["vibey"]
    assert vibey["sources"]["order"] == [first.id, SEED_CHAT.id]
    assert [model["id"] for model in vibey["models"]] == seeded
    assert announced == ["vibey"]


@pytest.mark.asyncio
async def test_the_first_source_seed_fetches_a_models_dev_copy_when_none_is_cached(tmp_path, monkeypatch):
    """MH-VIBEY-007: seeded rows keep the metadata they are written with.

    On a fresh install the first Source can arrive before any models.dev copy
    is cached, so the seed fetches one first rather than writing rows without
    the context window and output limit the Agent budgets with.
    """
    service, store, _announced = _seed_service(
        tmp_path, ModelHubConfig().to_payload(), [("claude", "claude-opus-5-5")],
    )
    copy_on_disk = {}
    service.models_dev_catalog = lambda: copy_on_disk

    def fetch_first_copy():
        copy_on_disk.update(SEED_CATALOG)
        return copy_on_disk

    monkeypatch.setattr("vibe.models_dev_catalog.load_models_dev_catalog", fetch_first_copy)
    await service._commit_new_source_locked(copy.deepcopy(SEED_ANTHROPIC))
    [row] = store.payload["agents"]["vibey"]["models"]
    assert (row["id"], row["context_window"], row["max_output_tokens"]) == ("claude-opus-5-5", 1_000_000, 128_000)


@pytest.mark.asyncio
async def test_mh_limits_001_rows_written_before_a_models_dev_copy_take_its_limits_once_it_arrives(
    tmp_path, monkeypatch,
):
    """MH-LIMITS-001: a row written with no models.dev copy cached gets its limits when one arrives.

    The seed's foreground fetch can fail while a background refresh lands later,
    and a picker add on a cold cache has no metadata either; the Agent then
    budgets the row with default limits. Only the limits a provider row lacks
    are filled: what a user, a seed or models.dev already stated stands, and
    built-in and manual rows are described by their CLI and their user.
    """
    catalog = {"anthropic": {"name": "Anthropic", "models": {
        "claude-opus-5-5": {"name": "Claude Opus 5.5", "limit": {"context": 1_000_000, "output": 128_000}},
        "claude-sonnet-5-5": {"name": "Claude Sonnet 5.5", "limit": {"context": 400_000, "output": 64_000}},
    }}}
    payload = ModelHubConfig().to_payload()
    payload["agents"]["claude"]["models"] = [
        {"id": "claude-opus-5-5", "origin": "builtin"},
        {"id": "claude-sonnet-5-5", "origin": "manual"},
    ]
    payload["agents"]["opencode"]["models"] = [
        # The user set the context window when adding it.
        {"id": "claude-sonnet-5-5", "origin": "provider", "native_protocol": "anthropic", "context_window": 200_000},
        # models.dev described it, and the user cleared both limits since.
        {
            "id": "claude-opus-5-5", "origin": "provider", "native_protocol": "anthropic",
            "models_dev_id": "anthropic/claude-opus-5-5",
        },
    ]
    payload["agents"]["codex"]["models"] = [{"id": "gpt-local-only", "origin": "provider"}]
    service, store, announced = _seed_service(tmp_path, payload, [("claude", "claude-opus-5-5")])
    cold = {}
    service.models_dev_catalog = lambda: cold

    def foreground_fetch_fails():
        raise RuntimeError("models.dev catalog is unavailable")

    monkeypatch.setattr("vibe.models_dev_catalog.load_models_dev_catalog", foreground_fetch_fails)
    await service._commit_new_source_locked(copy.deepcopy(SEED_ANTHROPIC))
    [seeded] = store.payload["agents"]["vibey"]["models"]
    assert (seeded["id"], seeded["context_window"], seeded["max_output_tokens"]) == ("claude-opus-5-5", None, None)
    assert announced == ["vibey"]
    before = copy.deepcopy(store.payload)

    assert await service.backfill_models_dev_limits() == []
    assert store.payload == before

    cold.update(catalog)
    assert await service.backfill_models_dev_limits() == ["vibey", "opencode"]

    def limits(backend):
        return [
            (row["id"], row["models_dev_id"], row["context_window"], row["max_output_tokens"])
            for row in store.payload["agents"][backend]["models"]
        ]

    assert limits("vibey") == [("claude-opus-5-5", "anthropic/claude-opus-5-5", 1_000_000, 128_000)]
    assert limits("opencode") == [
        ("claude-sonnet-5-5", "anthropic/claude-sonnet-5-5", 200_000, 64_000),
        ("claude-opus-5-5", "anthropic/claude-opus-5-5", None, None),
    ]
    for backend in ("claude", "codex"):
        assert store.payload["agents"][backend] == before["agents"][backend]
    # Committed as a catalog change, so each runtime takes it at its next turn.
    assert announced == ["vibey", "vibey", "opencode"]
    filled = copy.deepcopy(store.payload)
    assert await service.backfill_models_dev_limits() == []
    assert store.payload == filled


def test_native_cli_and_direct_are_not_vibey_channels(tmp_path):
    """MH-VIBEY-004: neither invalid persisted state nor routing can select a CLI."""
    native = _source("src_native001", "Native", channel="native_cli", vendor="anthropic", protocol="anthropic")
    service = _vibey_service(tmp_path, [native])
    config = service.store.load()
    assert not ModelHubConfig.source_eligible_for_backend(native, "vibey")
    agent = config.agents["vibey"]
    agent.routes["menu-alias"] = ModelHubRouteConfig((ModelHubRouteHopConfig(native.id, "shared-model"),))
    chain = service.agent_chain("vibey", "menu-alias")
    assert chain["current"] is None
    assert chain["supply_state"] == "interrupted"
    with pytest.raises(ValueError, match="ineligible"):
        ModelHubConfig.from_payload(config.to_payload())
    agent.routes.clear()
    agent.mode = "direct"
    with pytest.raises(ValueError, match="must be hub"):
        ModelHubConfig.from_payload(config.to_payload())
    assert bind_persisted_launch(SimpleNamespace(), {
        "backend": "vibey", "channel": "native_cli", "source_id": native.id, "target_model": "shared-model",
    }) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", PROTOCOL_ENDPOINTS)
@pytest.mark.parametrize("known", [False, True])
async def test_hop_resolution_uses_primary_protocol_and_alias_capabilities(tmp_path, protocol, known):
    """MH-VIBEY-001: the launch boundary owns protocol, identity and planning metadata."""
    source = _source("src_primary01", "Primary", protocol=protocol, model_id="upstream-model")
    service = _vibey_service(tmp_path, [source])
    if known:
        service.store.config.agents["vibey"].models[0] = ModelHubBackendModelConfig(
            id="menu-alias", context_window=128000, max_output_tokens=4096,
            input_modalities=["text", "image"], supports_tools=True, supports_reasoning=False,
            reasoning_efforts=["high"],
        )
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="vibey:test", turn_id="turn-hop")
        _validate("hop-resolution.schema.json", hop)
        assert hop["protocol"] == protocol
        assert hop["provider"] == "openai"
        assert hop["base_url"].endswith("/vibey/v1")
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
        _assert_mandatory_fields("hop-resolution.schema.json", hop)
        launch = await router.resolve("vibey", "menu-alias", process_scope="vibey:test", turn_id="turn-hop")
        assert hop["token"] not in repr(launch)
        assert build_claude_hub_env({"PATH": "/fixture"}, launch) == {"PATH": "/fixture"}
        assert build_codex_hub_launch(["fixture"], {}, launch) == (["fixture"], None)
        assert claude_settings_for_launch("{}", launch) == "{}"
        assert service.list_agents()[-1]["cli_present"] is False
        with pytest.raises(ModelHubError):
            await service.set_agent_mode("vibey", "direct")
        with pytest.raises(ModelHubError):
            await ModelHubRuntimeRouter(service=service).resolve("vibey", "menu-alias")
        with pytest.raises(ModelHubError):
            await resolve_model_hub_launch(SimpleNamespace(), "vibey", "menu-alias")
    finally:
        await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", PROTOCOL_ENDPOINTS)
@pytest.mark.parametrize("delivery", ["buffered", "stream", "settled"])
async def test_failover_origin_is_available_before_body_and_matches_served_attempt(tmp_path, protocol, delivery):
    """MH-VIBEY-002: HTTP headers identify the same fallback attempt that is persisted."""
    stream = delivery == "stream"
    fallback_protocol = "openai_responses" if protocol != "openai_responses" else "anthropic"
    first = _source("src_primary01", "Primary", vendor="custom", protocol=protocol)
    second = _source(
        "src_fallback01", "Fallback", vendor="anthropic", protocol=fallback_protocol, model_id="模型/β",
    )
    answer = b"{}" if delivery == "settled" else _body(protocol, stream)
    success = _outcome(RawOutcomeKind.SUCCESS, source_id=second.id, stream_started=stream)
    service = _vibey_service(tmp_path, [first, second], handles=[
        InvokeHandle(_outcome(RawOutcomeKind.HTTP_ERROR, status=429, code="rate_limit_error", source_id=first.id)),
        InvokeHandle(success) if delivery == "settled" else LiveInvokeHandle(success, (answer,)),
    ])
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="vibey:test", turn_id="turn-fallback")
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
        _assert_mandatory_fields("turn-provenance.schema.json", record)
        # The new Avibe promise must not tighten native or released records.
        # Exercise the persistence reader, not only in-memory schema payloads.
        history = []
        for backend in ("claude", "codex", "opencode"):
            for version in (5, 6, 7, 8, 9, 10):
                native = copy.deepcopy(record)
                native.update(agent=backend, contract_version=version, turn_id=f"{backend}-{version}")
                native["served"].pop("origin")
                for attempt in native["failed_attempts"]:
                    attempt.pop("origin", None)
                history.append(native)
        history_path = tmp_path / "native-history.json"
        history_path.write_text(json.dumps(history))
        history_store = BoundedProvenanceStore(history_path)
        for native in history:
            loaded = history_store.get(native["turn_id"])
            assert loaded == native
            _validate("turn-provenance.schema.json", loaded)
        assert record["served"]["origin"] == origin
        assert record["served"]["source_id"] == second.id
        assert record["failed_attempts"][0]["source_id"] == first.id
        assert [entry[0] for entry in service.adapter.invocations] == [first.id, second.id]
        assert all(request.protocol == protocol for request in service.adapter.requests)
        assert all(SERVED_HOP_HEADER not in request.headers for request in service.adapter.requests)
    finally:
        await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["vibey", "claude", "codex", "opencode"])
@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize(
    "failure,carrier,body_available,cause,expected_status,has_producer",
    [
        ("upstream_terminal", "resolved", True, "upstream", 400, True),
        ("local_spool", "resolved", True, "local", 502, True),
        ("bodyless_terminal", "error", False, "upstream", 400, True),
        ("bodyless_protocol", "error", False, "upstream", 502, True),
        ("bodyless_local", "error", False, "local", 502, False),
        ("exhaustion", "error", False, "local", 503, False),
        ("pre_admission", "error", False, "local", 502, False),
    ],
)
async def test_terminal_response_origin_follows_its_carrier(
    tmp_path, monkeypatch, backend, fallback, failure, carrier, body_available, cause,
    expected_status, has_producer,
):
    """MH-VIBEY-002: response origin follows evidence, not the last attempted hop.

    The old buffered-only test missed the error carrier. The feasible terminal
    matrix separates upstream errors from local endings, including local delivery
    failure after acquiring a known producer's body. Native clients stay unchanged.
    """
    source = _source(
        "src_primary01", "Primary", vendor="anthropic", protocol="anthropic",
        model_id="模型/β" if backend == "vibey" else "shared-model",
    )
    outcomes = {
        "upstream_terminal": _outcome(RawOutcomeKind.HTTP_ERROR, status=400, code="invalid_request_error", source_id=source.id),
        "local_spool": _outcome(RawOutcomeKind.SUCCESS, source_id=source.id),
        "bodyless_terminal": _outcome(RawOutcomeKind.HTTP_ERROR, status=400, code="invalid_request_error", source_id=source.id),
        "bodyless_protocol": _outcome(RawOutcomeKind.PROTOCOL_ERROR, source_id=source.id),
        "bodyless_local": _outcome(RawOutcomeKind.NETWORK_ERROR, status=502, code="engine_down", source_id=source.id),
        "exhaustion": _outcome(RawOutcomeKind.HTTP_ERROR, status=429, code="rate_limit_error", source_id=source.id),
        "pre_admission": _outcome(RawOutcomeKind.NETWORK_ERROR, status=502, code="engine_down", source_id=source.id),
    }
    handle = LiveInvokeHandle(outcomes[failure], (b"{}",)) if body_available else InvokeHandle(outcomes[failure])
    sources, handles = [source], [handle]
    if fallback:
        first = _source("src_fallback01", "First", vendor="custom", protocol="openai_chat")
        sources.insert(0, first)
        handles.insert(0, InvokeHandle(_outcome(
            RawOutcomeKind.HTTP_ERROR, status=429, code="rate_limit_error", source_id=first.id,
        )))
    service = _vibey_service(tmp_path, sources, handles=handles)
    observed_carriers = []
    resolve = service.resolve_with_recovery

    async def observe_carrier(**kwargs):
        try:
            result = await resolve(**kwargs)
        except ModelHubError:
            observed_carriers.append("error")
            raise
        observed_carriers.append("resolved")
        return result

    monkeypatch.setattr(service, "resolve_with_recovery", observe_carrier)
    if failure == "pre_admission":
        invoke = service.adapter.invoke

        async def fail_before_admission(*args, **kwargs):
            if args[0] == source.id:
                # Engine-local completion without admitting an upstream call.
                return InvokeHandle(outcomes[failure])
            return await invoke(*args, **kwargs)

        monkeypatch.setattr(service.adapter, "invoke", fail_before_admission)
    if failure == "local_spool":
        def unavailable_spool(*args, **kwargs):
            raise OSError("fixture spool unavailable")

        monkeypatch.setattr(
            "core.handlers.model_hub.turn_gateway.tempfile.SpooledTemporaryFile", unavailable_spool,
        )
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    turn_id = "turn-terminal-carrier"
    try:
        launch = await router.resolve(
            backend, service.store.requested_models[backend], process_scope=f"{backend}:test", turn_id=turn_id,
        )
        protocol = "anthropic" if backend in {"vibey", "claude"} else "openai_responses"
        # Avibe's frontend uses the primary's protocol; fallback can differ.
        if backend == "vibey":
            protocol = launch.protocol
        headers = {"Authorization": f"Bearer {launch.gateway_token}"}
        if launch.gateway_request_metadata:
            headers["x-codex-turn-metadata"] = json.dumps(launch.gateway_request_metadata)
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{launch.gateway_base_url}/v1/{PROTOCOL_ENDPOINTS[protocol]}",
                headers=headers,
                json={"model": launch.runtime_model, "stream": False, "messages": []},
            ) as response:
                assert response.status == expected_status
                origin = None
                if backend == "vibey" and has_producer:
                    origin = json.loads(response.headers[SERVED_HOP_HEADER])
                    assert origin == {"provider": "anthropic", "api": "anthropic", "model": "模型/β"}
                else:
                    assert SERVED_HOP_HEADER not in response.headers
                assert (await response.json())["error"]
        completion = router.settle_turn(
            turn_id, settled_by=SETTLED_BY_TERMINAL_RESULT, ts=NOW.isoformat(),
        )
        if completion:
            await completion
        assert observed_carriers == [carrier]
        assert [entry[0] for entry in service.adapter.invocations] == (
            ([sources[0].id] if fallback else []) + ([] if failure == "pre_admission" else [source.id])
        )
        record = service.provenance.get(turn_id)
        if backend == "opencode":
            # The native shared server has no exact turn discriminator.
            assert record is None
            return
        _validate("turn-provenance.schema.json", record)
        assert record["outcome"] == ("exhausted" if failure == "exhaustion" else "failed_terminal")
        if cause == "upstream":
            assert record["terminal_error"]["source_id"] == source.id
            if backend == "vibey":
                assert record["terminal_error"]["origin"] == origin
            else:
                assert "origin" not in record["terminal_error"]
        elif failure == "exhaustion":
            assert record["terminal_error"] is None
        else:
            # Existing engine-down provenance deliberately does not blame the
            # upstream Source for local delivery failure, despite a known producer.
            assert record["terminal_error"]["reason"] == "engine_down"
            assert record["terminal_error"]["source_id"] is None
    finally:
        await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("model_id,allowed", [
    pytest.param("a" * 4044, True, id="exact-bound"),  # 52 bytes of JSON envelope.
    pytest.param("a" * 4045, False, id="one-over"),
    pytest.param("模" * 1350, False, id="unicode-expansion"),
])
async def test_origin_header_limit_is_checked_before_each_vibey_hop(tmp_path, model_id, allowed, fallback):
    """MH-VIBEY-002: transport bounds must reject a hop before any upstream work.

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
    service = _vibey_service(tmp_path, sources, handles=handles)
    reloaded = ModelHubConfig.from_payload(service.store.config.to_payload())
    assert reloaded.sources[-1].models[0].id == model_id
    assert reloaded.agents["vibey"].routes["menu-alias"].hops[-1].model_id == model_id
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="vibey:test", turn_id="turn-header-bound")
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
    service = _vibey_service(tmp_path, [source], handles=[
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
        hop = await router.resolve_hop("menu-alias", process_scope="vibey:test", turn_id="turn-slow")
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
    service = _vibey_service(tmp_path, [first, second], handles=[
        LiveInvokeHandle(
            _outcome(RawOutcomeKind.SUCCESS, source_id=source.id, stream_started=True),
            (_body(source.protocol, True),),
        )
        for source in (first, second)
    ])
    agent = service.store.config.agents["vibey"]
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
            await router.resolve_hop(alias, process_scope=f"vibey:session-{index}", turn_id=f"turn-{index}")
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
