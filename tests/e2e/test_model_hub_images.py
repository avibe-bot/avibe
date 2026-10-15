"""MH-HOSTED-GATEWAY-007, once per release (``vibey-hosted-tools.md`` §7.4): each image path through the real engine.

A Vibey turn's ``image_generate`` call goes through the production gateway and
the pinned engine to a loopback upstream that answers as the vendor's images
endpoint does. Each path the bundled overlay offers must reach that upstream
with the request the gateway built, and come back as the canonical answer; an
image model the engine itself refuses must never be learned as the upstream's
refusal.

A model whose path is not here ships without an overlay entry. Run it with
``-m e2e_model_hub`` and ``VIBE_MODEL_HUB_ENGINE_MANIFEST_PATH`` set to an
offline manifest of the engine under test.
"""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager

import aiohttp
import pytest
from aiohttp import web

from core.handlers.model_hub import hosted_tools as hosted_tools_module
from core.handlers.model_hub.capability_evidence import EvidenceFeature
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from modules.agents.model_hub import ModelHubRuntimeRouter
from tests.e2e.test_model_hub_runtime import _isolated_engine_adapter
from tests.scenario_harness.model_hub import MemoryModelHubStore, service_for
from tests.test_model_hub_google import _server
from tests.test_model_hub_hosted_evidence import latest_ok
from tests.test_model_hub_l3 import _source
from tests.test_model_hub_vibey_consumer import _vibey_service
from vibe import backend_model_catalog

pytestmark = pytest.mark.e2e_model_hub

# A 1x1 PNG, as an images endpoint returns it in b64_json.
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


@asynccontextmanager
async def _image_turn(tmp_path, monkeypatch, *, vendor, protocol, chat_model, base_path, answer):
    """A Vibey turn on one API-key Source, through the production gateway and the pinned engine."""

    requests: list[dict] = []

    async def respond(request):
        requests.append({"path": request.path, "body": await request.json()})
        status, payload = answer
        return web.json_response(payload, status=status)

    with _isolated_engine_adapter(tmp_path / "engine", monkeypatch) as adapter:
        async with AsyncExitStack() as stack:
            root = await stack.enter_async_context(_server(respond))
            source = _source("src_imagee2e01", "Image key", vendor=vendor, protocol=protocol, model_id=chat_model)
            source.base_url = root + base_path
            source.credential_ref = adapter.state_store.store_api_key(
                "sk-synthetic-image-e2e", vendor=vendor, protocol=protocol, base_url=source.base_url,
            )
            (tmp_path / "service").mkdir()
            config = _vibey_service(tmp_path / "service", [source]).store.config
            service = service_for(tmp_path / "service", MemoryModelHubStore(config), adapter)
            await service.runtime_start()
            gateway = ModelHubTurnGateway(service)
            try:
                router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
                hop = await router.resolve_hop("menu-alias", process_scope="vibey:image", turn_id="turn-image-e2e")

                async def call(arguments):
                    async with aiohttp.ClientSession() as client:
                        async with client.post(
                            f'{hop["base_url"]}/hosted-tools/image_generate',
                            headers={"Authorization": f'Bearer {hop["token"]}', **hop["request_headers"]},
                            json={"arguments": arguments},
                        ) as response:
                            return response.status, await response.json(content_type=None)

                yield service, source, hop, call, requests
            finally:
                await gateway.close()


SERVED = (200, {"created": 1, "data": [{"b64_json": PNG}], "size": "1536x1024", "quality": "medium",
                "usage": {"input_tokens": 20, "output_tokens": 1056, "total_tokens": 1076}})


@pytest.mark.asyncio
@pytest.mark.parametrize(("vendor", "protocol", "chat_model", "base_path", "arguments", "upstream"), [
    # An OpenAI API key, which the engine serves through its Codex executor's direct Image API path.
    ("openai", "openai_responses", "gpt-6-astra", "/v1",
     {"model": "gpt-image-2", "prompt": "a lighthouse at dusk", "size": "1536x1024"},
     ("/v1/images/generations", {"model": "gpt-image-2", "prompt": "a lighthouse at dusk", "size": "1536x1024",
                                 "output_format": "jpeg", "output_compression": 90})),
    # An edit: the reference image goes as a data URL, and the engine routes the body to the edits endpoint.
    ("openai", "openai_responses", "gpt-6-astra", "/v1",
     {"model": "gpt-image-2", "prompt": "make it blue", "images": [PNG], "n": 1},
     ("/v1/images/edits", {"model": "gpt-image-2", "prompt": "make it blue", "n": 1,
                           "images": [{"image_url": f"data:image/png;base64,{PNG}"}],
                           "output_format": "jpeg", "output_compression": 90})),
    # A Gemini API key: Google's OpenAI-compatible images endpoint, which the engine serves for a flagged model.
    ("gemini", "openai_chat", "gemini-3-flash", "/v1beta/openai",
     {"model": "gemini-2.5-flash-image", "prompt": "a lighthouse at dusk", "size": "16:9", "n": 1},
     ("/v1beta/openai/images/generations", {"model": "gemini-2.5-flash-image", "prompt": "a lighthouse at dusk",
                                            "aspect_ratio": "16:9", "n": 1, "response_format": "b64_json"})),
], ids=["openai-generate", "openai-edit", "gemini-generate"])
async def test_each_offered_image_path_reaches_the_vendors_images_endpoint(
    tmp_path, monkeypatch, vendor, protocol, chat_model, base_path, arguments, upstream,
):
    """MH-HOSTED-GATEWAY-007: the offered model, the request the gateway built, and the canonical answer."""

    async with _image_turn(
        tmp_path, monkeypatch, vendor=vendor, protocol=protocol, chat_model=chat_model, base_path=base_path,
        answer=SERVED,
    ) as (service, source, hop, call, requests):
        assert arguments["model"] in {model["model_id"] for model in hop["hosted_tools"]["image_generate"]["models"]}
        status, answer = await call(arguments)
    assert status == 200, answer
    assert requests == [{"path": upstream[0], "body": upstream[1]}]
    assert answer["record"] == {"status": "completed", "sources": []}
    assert answer["images"] == [{"data": PNG}]
    assert answer["execution"] == {
        "mode": "direct",
        "executor_origin": {"provider": vendor, "api": protocol, "model": arguments["model"]},
        "executor_source_id": source.id,
        "executor_billing": "metered",
        **({"cost": answer["execution"]["cost"]} if "cost" in answer["execution"] else {}),
    }
    assert latest_ok(service, "image_generate") == (source.id, arguments["model"], "direct")


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["engine", "upstream"])
async def test_only_an_upstream_refusal_teaches_that_a_source_lacks_an_image_model(tmp_path, monkeypatch, refusal):
    """MH-HOSTED-GATEWAY-007: the engine's own refusal is never learned; the upstream's 404 is, for the endpoint."""

    if refusal == "engine":
        # xAI's image model as the overlay might list it: the pinned engine refuses it on its own.
        grok = backend_model_catalog.ImageModelSpec(
            "grok-imagine-image", "Grok Imagine", ("size",), ("ratio",), kinds=("api_key",),
        )
        monkeypatch.setattr(hosted_tools_module, "provider_image_models", lambda vendor: (grok,) if vendor == "xai" else ())
        vendor, chat_model, model = "xai", "grok-4.6", "grok-imagine-image"
    else:
        vendor, chat_model, model = "openai", "gpt-6-astra", "gpt-image-2"
    not_served = (404, {"error": {"message": "Not Found", "type": "invalid_request_error"}})
    async with _image_turn(
        tmp_path, monkeypatch, vendor=vendor, protocol="openai_responses", chat_model=chat_model, base_path="/v1",
        answer=not_served,
    ) as (service, source, _hop, call, requests):
        status, answer = await call({"model": model, "prompt": "a lighthouse"})
        refused = service.hosted_evidence().unsupported(source, model, EvidenceFeature.hosted("image_generate", "call"))
    assert status == 200 and answer["record"]["status"] == "failed", answer
    assert (len(requests), refused) == ((0, False) if refusal == "engine" else (1, True))
