"""Google's native wire at Model Hub's actual owning boundaries.

The existing three-protocol tests cannot detect a fourth protocol being admitted
but misrouted through a JSON model/stream field, losing Gemini terminal/usage
facts, or forwarding primary-origin thought signatures after fallback.
"""

from __future__ import annotations

import copy
import io
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
from aiohttp import web
import pytest
import yaml
from jsonschema import ValidationError

from config.v2_config import ModelHubModelConfig, ModelHubRouteConfig, ModelHubRouteHopConfig, ModelHubSourceConfig
from core.handlers.model_hub.adapter import ObservationOutcome, RawOutcomeKind, SourceBinding
from core.handlers.model_hub.provenance import SERVED_HOP_HEADER
from core.run_settlement import SETTLED_BY_TERMINAL_RESULT
from core.handlers.model_hub.stream_wire import (
    ProtocolSSEState,
    ProtocolUsageReport,
    observe_buffered_protocol_response,
)
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from core.handlers.model_hub.request import ModelHubRequest
from core.handlers.model_hub.service import ModelHubError
from modules.agents.model_hub import ModelHubRuntimeRouter
from tests.test_model_hub_avibe_consumer import _avibe_service, _validate
from tests.test_model_hub_l3 import InvokeHandle, LiveInvokeHandle, _outcome, _source
from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter
from vibe.model_hub_runtime.client import EngineClient, EngineClientError, EngineConnection, probe_models
from vibe.model_hub_runtime.config import render_engine_config
from vibe.model_hub_runtime.state import EngineStateStore, RuntimeSecrets, SourceRecord
from vibe.model_hub_runtime.state import EngineStateError


GOOGLE_RESPONSE = {
    "candidates": [{
        "content": {"role": "model", "parts": [{"text": "Answer: 上海"}]},
        "finishReason": "STOP",
        "index": 0,
    }],
    "usageMetadata": {
        "promptTokenCount": 12, "cachedContentTokenCount": 4,
        "candidatesTokenCount": 3, "thoughtsTokenCount": 2, "totalTokenCount": 17,
    },
}


def _sse(payload):
    return b"data: " + json.dumps(payload, ensure_ascii=False).encode() + b"\n\n"


@asynccontextmanager
async def _server(handler):
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        port = site._server.sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("suffix", ["", "/v1beta"])
def test_google_source_roundtrips_and_uses_native_engine_registration(tmp_path, suffix):
    """Persisted Source, runtime state and CPA YAML must agree on one protocol."""
    source = _source("src_google001", "Google", vendor="custom", protocol="google")
    source.base_url = f"https://upstream.invalid/proxy{suffix}"
    payload = source.to_payload()
    assert ModelHubSourceConfig.from_payload(payload).to_payload() == payload
    _validate("source.schema.json", payload)
    store = EngineStateStore(tmp_path / "state")
    credential = store.store_api_key(
        "synthetic-google-key", vendor="custom", protocol="google", base_url=source.base_url,
    )
    record = SourceRecord(
        source_id=source.id, vendor="custom", protocol="google", base_url=source.base_url,
        credential_ref=credential, allowed_origins=(), model_ids=("gemini-fixture",), prefix="google-fixture",
    )
    store.replace_sources([record])
    assert store.list_sources() == [record]
    rendered = yaml.safe_load(render_engine_config(
        tmp_path / "config.yaml", host="127.0.0.1", port=12345, auth_dir=tmp_path / "auth",
        runtime_secrets=RuntimeSecrets("synthetic-management", "synthetic-gateway"),
        sources=[record], state_store=store,
    ))
    assert rendered["gemini-api-key"] == [{
        "api-key": "synthetic-google-key", "prefix": "google-fixture",
        "base-url": "https://upstream.invalid/proxy",
        "models": [{"name": "gemini-fixture", "alias": "gemini-fixture", "display-name": "gemini-fixture #0"}],
    }]
    assert "openai-compatibility" not in rendered


@pytest.mark.parametrize("vendor", ["relay-x", "openai"])
@pytest.mark.parametrize("boundary", ["config", "schema", "create-schema", "observe", "create", "save", "runtime"])
async def test_google_vendor_refusal_is_shared_by_source_admission(tmp_path, vendor, boundary):
    """Unpinned vendors must not bypass Google's custom-only provider identity."""
    source = _source("src_google001", "Google", vendor=vendor, protocol="google")
    source.base_url = "https://upstream.invalid"
    draft = {
        "vendor": vendor, "protocol": "google", "base_url": source.base_url, "key": "synthetic-key",
    }
    if boundary == "config":
        with pytest.raises(ValueError, match="Google"):
            ModelHubSourceConfig.from_payload(source.to_payload())
    elif boundary in {"schema", "create-schema"}:
        with pytest.raises(ValidationError) as refused:
            _validate(
                "source.schema.json" if boundary == "schema" else "source-create.schema.json",
                source.to_payload() if boundary == "schema" else draft,
            )
        assert list(refused.value.absolute_path) == ["vendor"]
    elif boundary == "runtime":
        store = EngineStateStore(tmp_path / "state")
        credential = store.store_api_key(
            "synthetic-key", vendor=vendor, protocol="google", base_url=source.base_url,
        )
        with pytest.raises(EngineStateError, match="Google"):
            store.sync_sources([SourceBinding(
                source_id=source.id, vendor=vendor, protocol="google", base_url=source.base_url,
                credential_ref=credential, allowed_origins=(), model_ids=("gemini-fixture",),
            )])
        assert store.list_sources() == []
    else:
        from tests.test_model_hub_api import _service

        service, store, adapter = _service(tmp_path)
        with pytest.raises(ModelHubError):
            if boundary == "observe":
                await service.observe_source(draft)
            else:
                await service.create_source({
                    **draft, "kind": "api_key", "save_unverified": boundary == "save",
                })
        assert store.config.sources == []
        assert adapter.credential_count == 0


@pytest.mark.parametrize("base_url", [None, "https://upstream.invalid/v1beta?api-version=fixture"])
def test_google_target_refuses_unrenderable_roots_before_runtime_state_change(tmp_path, base_url):
    """Google has no vendor-default root, and CPA appends paths rather than joining query URLs."""
    store = EngineStateStore(tmp_path / "state")
    credential = store.store_api_key("synthetic-key", vendor="custom", protocol="google", base_url=base_url)
    with pytest.raises(EngineStateError, match="Google"):
        store.sync_sources([SourceBinding(
            source_id="src_google001", vendor="custom", protocol="google", base_url=base_url,
            credential_ref=credential, allowed_origins=(), model_ids=("gemini-fixture",),
        )])
    assert store.list_sources() == []


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("upstream_protocol", ["google", "anthropic", "openai_responses", "openai_chat"])
@pytest.mark.parametrize("model_id", ["vendor/model-中文", "../fixture", "/fixture", "nested/../fixture"])
async def test_google_engine_frontend_addresses_routed_model_in_url(stream, upstream_protocol, model_id):
    """CPA selects the upstream using its path, regardless of Source protocol."""
    received = []

    async def handle(request):
        received.append((request.path, dict(request.query), await request.json(), dict(request.headers)))
        return web.Response(
            body=_sse(GOOGLE_RESPONSE) if stream else json.dumps(GOOGLE_RESPONSE).encode(),
            content_type="text/event-stream" if stream else "application/json",
        )

    async with _server(handle) as root:
        client = EngineClient(EngineConnection(root, "unused-management", "synthetic-gateway"))
        source = SourceRecord(
            source_id="src_google001", vendor="custom", protocol=upstream_protocol,
            base_url="https://upstream.invalid", credential_ref="cred_google001",
            allowed_origins=(), model_ids=(model_id,), prefix="fixture",
        )
        payload = {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}
        handle = await client.invoke(source, model_id, payload, stream=stream, request_protocol="google")
        assert handle.stream is not None
        raw = b"".join([chunk async for chunk in handle.stream])
        await handle.close_stream()
        outcome = await handle.outcome()
        assert outcome.kind == RawOutcomeKind.SUCCESS
        assert outcome.usage == ProtocolUsageReport(input_tokens=12, cached_input_tokens=4, output_tokens=5)
        assert b"Answer" in raw
    path, query, body, headers = received[0]
    action = "streamGenerateContent" if stream else "generateContent"
    assert path == f"/v1beta/models/fixture/{model_id}:{action}"
    assert query == ({"alt": "sse"} if stream else {})
    assert body == payload
    assert headers["Authorization"] == "Bearer synthetic-gateway"


@pytest.mark.parametrize(("payload", "verified"), [
    (b'{"promptFeedback":{"blockReason":"SAFETY"}}', True),
    (b'{"promptFeedback":{"blockReason":"OTHER"},"usageMetadata":{"promptTokenCount":4}}', True),
    (b'{"promptFeedback":{"blockReason":""}}', False),
    (b'{"promptFeedback":{"blockReason":null}}', False),
    (b'{"promptFeedback":{"blockReason":"SAFETY"},"promptFeedback":{}}', False),
    (b'{"promptFeedback":{"blockReason":"SAFETY"}', False),
    (b'{"promptFeedback":{"blockReason":"SAFETY"},"error":{"code":503}}', False),
])
async def test_google_buffered_prompt_block_proves_recovery_without_candidates(payload, verified):
    """A native blocked completion restores health; metadata/malformed/error bodies cannot."""
    async def handle(request):
        return web.Response(body=payload, content_type="application/json")

    async with _server(handle) as root:
        source = SourceRecord(
            source_id="src_google001", vendor="custom", protocol="google", base_url=root,
            credential_ref="cred_google001", allowed_origins=(), model_ids=("fixture",), prefix="fixture",
        )
        client = EngineClient(EngineConnection(root, "unused", "synthetic-key"))
        result = await client.invoke(source, "fixture", {"contents": []}, stream=False)
        assert (await result.outcome()).recovery_verified is verified
        await result.close_stream()


def test_google_stream_facts_do_not_confuse_metadata_with_output_or_drop_usage():
    """Google has no [DONE]; parts and finishReason carry the distinct facts."""
    state = ProtocolSSEState("google")
    state.observe(_sse({"modelVersion": "gemini-fixture", "usageMetadata": {"promptTokenCount": 12}}))
    assert not state.model_output_started
    assert state.terminal_observation() is None
    state.observe(_sse({"candidates": [{"content": {"role": "model", "parts": [
        {"functionCall": {"name": "lookup", "args": {"city": "上海"}}},
    ]}}]}))
    assert state.model_output_started
    assert state.terminal_observation() is None
    state.observe(_sse(GOOGLE_RESPONSE))
    terminal = state.terminal_observation()
    assert terminal.outcome == "served"
    assert terminal.usage == ProtocolUsageReport(input_tokens=12, cached_input_tokens=4, output_tokens=5)
    buffered = observe_buffered_protocol_response("google", io.BytesIO(json.dumps(GOOGLE_RESPONSE).encode()))
    assert buffered.recovery_verified
    assert buffered.usage == terminal.usage
    # Pinned CPA's Chat translator can emit a usage-only frame after finishReason.
    state.observe(_sse({"candidates": [], "usageMetadata": {
        "promptTokenCount": 20, "cachedContentTokenCount": 5, "candidatesTokenCount": 9,
    }}))
    assert state.terminal_observation().usage == ProtocolUsageReport(
        input_tokens=20, cached_input_tokens=5, output_tokens=9,
    )


@pytest.mark.parametrize("event_name", ["", "event: error\n"])
def test_google_stream_error_is_terminal_not_a_success(event_name):
    """Both Gemini-native and CPA's named error envelope must stop delivery."""
    state = ProtocolSSEState("google")
    state.observe(event_name.encode() + _sse({
        "error": {"code": 503, "status": "UNAVAILABLE", "message": "fixture failure"},
    }))
    assert state.terminal_observation().outcome == "failed_terminal"
    assert not state.model_output_started


@pytest.mark.parametrize(("status", "code"), [(400, "invalid_request_error"), (429, "rate_limit_error"), (503, "server_error")])
@pytest.mark.parametrize("stream", [False, True])
async def test_google_native_error_codes_reach_existing_retry_classification(status, code, stream):
    """HTTP-200 error bodies must classify the same as their native Google status."""
    async def handle(request):
        payload = {"error": {"code": status, "message": "synthetic-error"}}
        return web.Response(
            body=_sse(payload) if stream else json.dumps(payload).encode(),
            content_type="text/event-stream" if stream else "application/json",
        )

    async with _server(handle) as root:
        client = EngineClient(EngineConnection(root, "unused", "synthetic-key"))
        source = SourceRecord(
            source_id="src_google001", vendor="custom", protocol="google", base_url=root,
            credential_ref="cred_google001", allowed_origins=(), model_ids=("gemini-fixture",), prefix="google",
        )
        result = await client.invoke(source, "gemini-fixture", {"contents": []}, stream=stream)
        outcome = await result.outcome()
        assert outcome.kind == RawOutcomeKind.HTTP_ERROR
        assert outcome.error_code == code
        assert not outcome.stream_started
        await result.close_stream()


@pytest.mark.parametrize(("stream", "fallback_protocol", "same_origin"), [
    (stream, protocol, same_origin)
    for stream in (False, True)
    for protocol in ("google", "anthropic", "openai_responses", "openai_chat")
    for same_origin in (False, True)
    if not (stream and protocol == "openai_responses" and not same_origin)
])
async def test_google_router_gateway_failover_preserves_visible_history_and_origin(
    tmp_path, stream, fallback_protocol, same_origin,
):
    """A real Google request resolves path identity, strips opacity, and reports the winning admission."""
    primary = _source("src_google001", "Primary", vendor="custom", protocol="google")
    fallback = _source(
        "src_fallback01", "Fallback", vendor="custom", protocol=fallback_protocol, model_id="fallback-model",
    )
    if same_origin:
        fallback.protocol = "google"
        fallback.models[0].id = primary.models[0].id
    raw = _sse(GOOGLE_RESPONSE) if stream else json.dumps(GOOGLE_RESPONSE).encode()
    service = _avibe_service(tmp_path, [primary, fallback], handles=[
        InvokeHandle(_outcome(RawOutcomeKind.HTTP_ERROR, status=429, code="rate_limit_error")),
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=fallback.id), (raw,)),
    ])
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    signed = {"contents": [
        {"role": "model", "parts": [
            {"text": "Visible thought", "thought": True, "thoughtSignature": "opaque-thought"},
            {"functionCall": {"name": "verify", "args": {"signature": "user-data"}}, "thoughtSignature": "opaque-call"},
        ]},
        {"role": "user", "parts": [{"functionResponse": {"name": "verify", "response": {"signature": "user-data"}}}]},
    ]}
    plain = copy.deepcopy(signed)
    plain["contents"][0]["parts"][0] = {"text": "Visible thought"}
    plain["contents"][0]["parts"][1].pop("thoughtSignature")
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="avibe:google", turn_id="turn-google")
        _validate("hop-resolution.schema.json", hop)
        assert hop["base_url"].endswith("/avibe/v1beta")
        action = "streamGenerateContent?alt=sse" if stream else "generateContent"
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f'{hop["base_url"]}/models/{hop["runtime_model"]}:{action}',
                json=signed, headers={"x-goog-api-key": hop["token"]},
            ) as response:
                assert response.status == 200, await response.text()
                assert await response.read() == raw
                origin = json.loads(response.headers[SERVED_HOP_HEADER])
                assert origin == {
                    "provider": "custom", "api": fallback.protocol, "model": fallback.models[0].id,
                }
        assert service.adapter.requests[0] == signed
        assert service.adapter.requests[1] == (signed if same_origin else plain)
        assert all(request.protocol == "google" for request in service.adapter.requests)
        completion = router.settle_turn(
            "turn-google", settled_by=SETTLED_BY_TERMINAL_RESULT, ts="2026-10-02T08:00:00Z",
        )
        if completion:
            await completion
        record = service.provenance.get("turn-google")
        _validate("turn-provenance.schema.json", record)
        assert record["served"]["origin"] == origin
    finally:
        await gateway.close()


@pytest.mark.parametrize("public", [False, True])
async def test_google_observation_uses_paginated_credential_witness_without_inference(tmp_path, public):
    """Only a declared, credential-gated listing proves auth; every page is truthful inventory."""
    requests = []

    async def handle(request):
        requests.append((request.method, request.path, dict(request.query), request.headers.get("x-goog-api-key")))
        assert request.method == "GET"
        assert request.path == "/proxy/v1beta/models"
        if not public and request.headers.get("x-goog-api-key") is None:
            return web.json_response({"error": {"code": 403, "status": "PERMISSION_DENIED"}}, status=403)
        if request.query.get("pageToken") == "next +/中文":
            return web.json_response({"models": [{"name": "models/gemini-second"}]})
        return web.json_response({
            "models": [{"name": "models/gemini-first"}], "nextPageToken": "next +/中文",
        })

    async with _server(handle) as root:
        store = EngineStateStore(tmp_path / "state")
        base = root + "/proxy/v1beta"
        credential = store.store_api_key("synthetic-key", vendor="custom", protocol="google", base_url=base)
        adapter = CLIProxyEngineAdapter(supervisor=SimpleNamespace(), state_store=store)
        observed = await adapter.observe_source("custom", base, credential, ("google",))
        if public:
            assert observed.outcome != ObservationOutcome.OBSERVED
        else:
            assert observed.outcome == ObservationOutcome.OBSERVED
            assert [model.id for model in observed.models] == ["gemini-first", "gemini-second"]
        models = await probe_models(vendor="custom", protocol="google", base_url=base, secret="synthetic-key")
        assert [model.id for model in models] == ["gemini-first", "gemini-second"]
    assert all(method == "GET" for method, *_ in requests)
    assert all(query.keys() <= {"pageToken"} for _, _, query, _ in requests)


@pytest.mark.parametrize("bad_page", [
    b'{}', b'{"models":[null]}', b'{"models":[{"name":"gemini-invalid"}]}',
    b'{"models":[{"name":"models/good","name":null}]}',
    b'{"models":[],"nextPageToken":42}',
    b'{"models":[],"nextPageToken":"repeat"}',
])
async def test_google_discovery_never_publishes_partial_or_repeated_inventory(bad_page):
    """A bad continuation must not replace saved inventory with its first page."""
    calls = 0

    async def handle(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return web.json_response({"models": [{"name": "models/first"}], "nextPageToken": "repeat"})
        return web.Response(body=bad_page, content_type="application/json")

    async with _server(handle) as root:
        with pytest.raises(EngineClientError):
            await probe_models(vendor="custom", protocol="google", base_url=root, secret="synthetic-key")
    assert calls == 2


async def test_google_discovery_does_not_follow_redirects_with_credentials():
    """Page tokens are data; redirects must never move a secret to another target."""
    destination_requests = []

    async def destination(request):
        destination_requests.append(request)
        return web.json_response({"models": []})

    async with _server(destination) as other:
        async def redirect(request):
            return web.Response(status=302, headers={"Location": other + "/capture"})

        async with _server(redirect) as root:
            with pytest.raises(EngineClientError, match="HTTP 302"):
                await probe_models(vendor="custom", protocol="google", base_url=root, secret="synthetic-key")
    assert destination_requests == []


@pytest.mark.parametrize(("suffix", "payload", "token_valid", "status"), [
    (":generateContent", {}, False, 401),
    (":countTokens", {}, True, 404),
    (":streamGenerateContent?alt=json", {}, True, 400),
    (":streamGenerateContent?alt=sse&alt=json", {}, True, 400),
    (":generateContent", {"model": "another-model"}, True, 400),
    (":generateContent", {"stream": True}, True, 400),
])
async def test_google_local_errors_are_native_and_never_admit_a_hop(tmp_path, suffix, payload, token_valid, status):
    """Native endpoint/query/body validation shares all pre-admission no-origin rules."""
    service = _avibe_service(tmp_path, [_source("src_google001", "Google", vendor="custom", protocol="google")], handles=[])
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="avibe:google-errors", turn_id="turn-invalid")
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f'{hop["base_url"]}/models/{hop["runtime_model"]}{suffix}', json=payload,
                headers={"x-goog-api-key": hop["token"] if token_valid else "invalid"},
            ) as response:
                assert response.status == status
                assert SERVED_HOP_HEADER not in response.headers
                error = (await response.json())["error"]
                assert error["code"] == status
                assert isinstance(error["status"], str)
                assert error["message"]
        assert service.adapter.invocations == []
    finally:
        await gateway.close()


async def test_google_saved_source_probe_speaks_the_declared_protocol(tmp_path):
    """Add-time discovery is not inference; an explicit model test is and needs Gemini's body."""
    service = _avibe_service(tmp_path, [_source("src_google001", "Google", vendor="custom", protocol="google")], handles=[
        InvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id="src_google001")),
    ])
    result = await service.probe_source("src_google001", {"model": "shared-model"})
    _validate("source-probe-result.schema.json", result)
    assert service.adapter.requests == [{
        "contents": [{"role": "user", "parts": [{"text": "ping"}]}],
        "generationConfig": {"maxOutputTokens": 128},
    }]


@pytest.mark.parametrize("working_fallback", [False, True])
@pytest.mark.parametrize(("protocol", "model_id", "stream", "reason"), [
    ("openai_responses", "shared-model", True, "google_stream_responses_unsupported"),
] + [
    (protocol, "fixture:free", stream, "google_model_path_unsupported")
    for protocol in ("google", "anthropic", "openai_chat", "openai_responses")
    for stream in (False, True)
])
async def test_google_skips_unsupported_hops_before_admission(
    tmp_path, caplog, working_fallback, protocol, model_id, stream, reason,
):
    """Unsupported conversion is a local skip, never an upstream failure or false success."""
    import logging

    caplog.set_level(logging.INFO, logger="core.handlers.model_hub.service")
    blocked = _source("src_blocked01", "Blocked", vendor="custom", protocol=protocol, model_id=model_id)
    good = _source("src_google001", "Google", vendor="custom", protocol="google", model_id="gemini-fixture")
    raw = _sse(GOOGLE_RESPONSE) if stream else json.dumps(GOOGLE_RESPONSE).encode()
    service = _avibe_service(tmp_path, [blocked, good] if working_fallback else [blocked], handles=[
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=good.id), (raw,)),
    ])
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="google:unsupported", turn_id="unsupported")
        base = hop["base_url"].rsplit("/", 1)[0] + "/v1beta"
        action = "streamGenerateContent?alt=sse" if stream else "generateContent"
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f'{base}/models/{hop["runtime_model"]}:{action}',
                headers={"x-goog-api-key": hop["token"]},
                json={"contents": [{"role": "user", "parts": [{"text": "hello"}]}]},
            ) as response:
                if working_fallback:
                    assert response.status == 200
                    assert json.loads(response.headers[SERVED_HOP_HEADER])["api"] == "google"
                    assert await response.read() == raw
                else:
                    assert response.status == 422
                    assert SERVED_HOP_HEADER not in response.headers
                    assert (await response.json())["error"]["details"] == [
                        {"reason": reason},
                    ]
        assert [call[0] for call in service.adapter.invocations] == ([good.id] if working_fallback else [])
        assert blocked.state.status == "standby"
        assert any(getattr(record, "reason", None) == reason for record in caplog.records)
        completion = router.settle_turn(
            "unsupported", settled_by=SETTLED_BY_TERMINAL_RESULT, ts="2026-10-02T09:00:00Z",
        )
        if completion:
            await completion
        record = service.provenance.get("unsupported")
        _validate("turn-provenance.schema.json", record)
        assert record["failed_attempts"] == []
        assert record["served"]["source_id"] == good.id if working_fallback else record["served"] is None
    finally:
        await gateway.close()


@pytest.mark.parametrize(("backend", "protocol", "model_id"), [
    ("codex", "google", "shared-model"),
    ("codex", "google", "fixture:free"),
    ("avibe", "openai_chat", "fixture:free"),
])
async def test_other_frontends_and_native_paths_keep_their_admission_policy(tmp_path, backend, protocol, model_id):
    """The Google limitations must not silently broaden to native/other frontends."""
    source = _source("src_blocked01", "Native", protocol="openai_responses", model_id=model_id)
    service = _avibe_service(tmp_path, [source], handles=[
        InvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=source.id)),
    ])
    await service.resolve(
        backend=backend, model_id=service.store.requested_models[backend],
        request=ModelHubRequest({"contents": []}, protocol=protocol), stream=True,
    )
    assert [call[0] for call in service.adapter.invocations] == [source.id]


async def test_google_unsupported_model_skip_keeps_next_model_on_the_same_source(tmp_path):
    """The colon restriction belongs to the exact hop, not the Source's healthy inventory."""
    source = _source("src_google001", "Google", vendor="custom", protocol="google", model_id="fixture:free")
    source.models.append(ModelHubModelConfig(id="safe-model", provenance="discovered"))
    service = _avibe_service(tmp_path, [source], handles=[
        InvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=source.id)),
    ])
    service.revocations.add("src_retired01", "cred_retired01")
    service.store.config.agents["avibe"].routes["menu-alias"] = ModelHubRouteConfig((
        ModelHubRouteHopConfig(source.id, "fixture:free"),
        ModelHubRouteHopConfig(source.id, "safe-model"),
    ))
    result = await service.resolve(
        backend="avibe", model_id="menu-alias",
        request=ModelHubRequest({"contents": []}, protocol="google"), stream=False,
    )
    assert result.model_id == "safe-model"
    assert service.adapter.invocations == [(source.id, "safe-model", "avibe")]
    assert service.revocations.list() == []


@pytest.mark.parametrize("caller", ["turn", "agent_probe"])
@pytest.mark.parametrize("pending_revocation", [False, True])
async def test_google_unsupported_model_refusal_precedes_engine_work(tmp_path, caller, pending_revocation):
    """Local skips must not start an engine or alter custody before refusing."""
    source = _source("src_google001", "Google", vendor="custom", protocol="google", model_id="fixture:free")
    service = _avibe_service(tmp_path, [source])
    if pending_revocation:
        service.revocations.add("src_retired01", "cred_retired01")
    pending = service.revocations.list()
    before = copy.deepcopy(service.store.config.to_payload())
    generation = service._next_settlement_generation
    service._prepare_engine_for_demand = AsyncMock(wraps=service._prepare_engine_for_demand)
    service._ensure_engine_synced = AsyncMock(wraps=service._ensure_engine_synced)
    with pytest.raises(ModelHubError) as refused:
        if caller == "agent_probe":
            await service.probe_agent("avibe", "menu-alias")
        else:
            await service.resolve(
                backend="avibe", model_id="menu-alias",
                request=ModelHubRequest({"contents": []}, protocol="google"), stream=False,
            )
    assert refused.value.code == "google_model_path_unsupported"
    assert refused.value.status == 422
    assert refused.value.origin is None
    service._prepare_engine_for_demand.assert_not_awaited()
    service._ensure_engine_synced.assert_not_awaited()
    assert service.adapter.invocations == []
    assert service.store.config.to_payload() == before
    assert service.revocations.list() == pending
    assert service._next_settlement_generation == generation
