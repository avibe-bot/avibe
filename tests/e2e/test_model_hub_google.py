"""Google frontend against the shipped CPA binary, never a live vendor.

Unit transport mocks cannot prove CPA registration or translation: this layer
checks the real gateway/router/adapter/engine with only loopback fixture traffic.
"""

from __future__ import annotations

import json
from urllib.parse import quote

import aiohttp
from aiohttp import web
import pytest

from core.handlers.model_hub.provenance import SERVED_HOP_HEADER
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from core.run_settlement import SETTLED_BY_TERMINAL_RESULT
from modules.agents.model_hub import ModelHubRuntimeRouter
from tests.e2e.drivers.mock_llm_upstream import _buffered_response, _stream_frames
from tests.e2e.test_model_hub_runtime import _isolated_engine_adapter
from tests.scenario_harness.model_hub import MemoryModelHubStore, service_for
from tests.test_model_hub_vibey_consumer import _vibey_service
from tests.test_model_hub_google import GOOGLE_RESPONSE, _server, _sse
from tests.test_model_hub_l3 import _source


pytestmark = pytest.mark.e2e_model_hub


@pytest.mark.parametrize(("frontend", "protocol", "model_id"), [
    (frontend, protocol, "fixture-model")
    for frontend, protocol in [
        ("google", "google"), ("google", "anthropic"), ("google", "openai_responses"), ("google", "openai_chat"),
        ("anthropic", "google"), ("openai_responses", "google"), ("openai_chat", "google"),
    ]
] + [
    # CPA must receive the literal registered identity after HTTP normalization;
    # a Python loopback endpoint alone cannot prove the engine's route match.
    ("google", "openai_chat", model_id) for model_id in (
        "../fixture", "/fixture", "nested/../fixture", "fixture:free",
    )
])
@pytest.mark.parametrize("stream", [False, True])
async def test_real_google_frontend_uses_pinned_engine_and_reports_serving_origin(
    tmp_path, monkeypatch, frontend, protocol, model_id, stream,
):
    requests = []

    async def upstream(request):
        body = await request.json()
        requests.append((request.path, body, dict(request.headers)))
        google = protocol == "google"
        streaming = "streamGenerateContent" in request.path if google else body.get("stream")
        raw = (
            _sse(GOOGLE_RESPONSE) if google else b"".join(_stream_frames(protocol, body))
        ) if streaming else json.dumps(GOOGLE_RESPONSE if google else _buffered_response(protocol, body)).encode()
        if protocol == "openai_chat" and streaming:
            # The general three-protocol mock ends with [DONE] alone. Gemini
            # translation requires the upstream's native finish_reason too.
            raw = raw.replace(b"data: [DONE]", _sse({
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }) + b"data: [DONE]")
        return web.Response(body=raw, content_type="text/event-stream" if streaming else "application/json")

    with _isolated_engine_adapter(tmp_path, monkeypatch) as adapter:
        async with _server(upstream) as root:
            source = _source("src_google001", "Fixture", vendor="custom", protocol=protocol, model_id=model_id)
            source.base_url = root + ("/v1" if protocol == "openai_responses" else "")
            source.credential_ref = adapter.state_store.store_api_key(
                "synthetic-google-integration", vendor="custom", protocol=protocol, base_url=source.base_url,
            )
            config = _vibey_service(tmp_path, [source]).store.config
            service = service_for(tmp_path, MemoryModelHubStore(config), adapter)
            await service.runtime_start()
            gateway = ModelHubTurnGateway(service)
            router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
            try:
                hop = await router.resolve_hop("menu-alias", process_scope="vibey:real-google", turn_id="real-google")
                # The upstream determines the primary frontend. Ask the same
                # admitted gateway for Google to exercise CPA's conversion.
                base = hop["base_url"].rsplit("/", 1)[0]
                method = "streamGenerateContent?alt=sse" if stream else "generateContent"
                prompt = "真实 fixture: 上海"
                if frontend == "google":
                    endpoint = f'/v1beta/models/{quote(hop["runtime_model"], safe="")}:{method}'
                    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
                else:
                    endpoint = "/v1/" + {
                        "anthropic": "messages", "openai_responses": "responses", "openai_chat": "chat/completions",
                    }[frontend]
                    body = {"model": hop["runtime_model"], "stream": stream}
                    if frontend == "openai_responses":
                        body.update(input=prompt, max_output_tokens=128)
                    else:
                        body.update(messages=[{"role": "user", "content": prompt}], max_tokens=128)
                async with aiohttp.ClientSession() as client:
                    async with client.post(
                        base + endpoint,
                        headers={"Authorization": f'Bearer {hop["token"]}'}, json=body,
                    ) as response:
                        raw = await response.read()
                        unsupported_reason = None
                        if frontend == "google" and ":" in model_id:
                            unsupported_reason = b"google_model_path_unsupported"
                        elif frontend == "google" and protocol == "openai_responses" and stream:
                            unsupported_reason = b"google_stream_responses_unsupported"
                        if unsupported_reason is not None:
                            assert response.status == 422, raw
                            assert SERVED_HOP_HEADER not in response.headers
                            assert unsupported_reason in raw
                            assert requests == []
                            return
                        assert response.status == 200, raw
                        if frontend == "google":
                            assert b"candidates" in raw
                        assert b"mock response" in raw or b"Answer" in raw
                        origin = json.loads(response.headers[SERVED_HOP_HEADER])
                        assert origin == {"provider": "custom", "api": protocol, "model": model_id}
                completion = router.settle_turn(
                    "real-google", settled_by=SETTLED_BY_TERMINAL_RESULT, ts="2026-10-02T09:00:00Z",
                )
                if completion:
                    await completion
                record = service.provenance.get("real-google")
                assert record["outcome"] == "served", (record, raw)
                assert record["served"]["origin"] == origin
                assert len(requests) == 1
                path, body, headers = requests[0]
                assert "真实 fixture: 上海" in json.dumps(body, ensure_ascii=False)
                if protocol == "google":
                    assert path == f"/v1beta/models/{model_id}:{method.split('?')[0]}"
                    assert headers["X-Goog-Api-Key"] == "synthetic-google-integration"
                    # The pinned executor injects model and safetySettings;
                    # frontend routing still comes only from the URL.
                    assert body["model"] == model_id
                    assert "stream" not in body
                else:
                    assert body["model"] == model_id
                    assert path == {
                        "anthropic": "/v1/messages", "openai_responses": "/v1/responses",
                        "openai_chat": "/v1/chat/completions",
                    }[protocol]
            finally:
                await gateway.close()
