"""MH-HOSTED-GATEWAY-007: ``image_generate`` direct execution at the gateway (``vibey-hosted-tools.md`` §7.2, §7.4).

Each case posts an ``image_generate`` call to the real gateway endpoint for a
Vibey turn, with a recording engine adapter answering as the images endpoint
does; ``tests/e2e/test_model_hub_images.py`` runs the same paths through the
real engine. The turn's enum comes from the bundled overlay.
"""

from __future__ import annotations

import base64
import json
import struct
from dataclasses import replace

import aiohttp
import pytest

from config.v2_config import ModelHubHostedToolConfig
from core.handlers.model_hub import hosted_images as images_module
from core.handlers.model_hub import hosted_tools as hosted_tools_module
from core.handlers.model_hub.adapter import RawOutcomeKind
from core.handlers.model_hub.capability_evidence import EvidenceFeature
from core.handlers.model_hub.stream_wire import ProtocolUsageReport
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from modules.agents.model_hub import ModelHubRuntimeRouter
from tests.agent_core.tools.test_read import _png
from tests.test_model_hub_hosted_evidence import latest_ok
from tests.test_model_hub_l3 import InvokeHandle, LiveInvokeHandle, _outcome, _source, _usage_of
from tests.test_model_hub_vibey_consumer import _vibey_service
from vibe.backend_model_catalog import HostedToolPrice, ImageModelPrice, ImageModelSpec

OPENAI = "src_oaiimage01"
GEMINI = "src_gemimage01"
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
IMAGE = "image_generate"
FEATURE = EvidenceFeature.hosted(IMAGE, "call")


def _sources():
    return [
        _source(OPENAI, "OpenAI key", vendor="openai", protocol="openai_responses", model_id="gpt-6-astra"),
        _source(GEMINI, "Gemini key", vendor="gemini", protocol="openai_chat", model_id="gemini-3-flash"),
    ]


def _served(*images, size="1024x1024", quality="medium", usage=None, source_id=OPENAI):
    body = {"created": 1, "data": [{"b64_json": data} for data in images], "size": size, "quality": quality}
    tokens = usage or ProtocolUsageReport.of(input_tokens=20, cached_input_tokens=0, output_tokens=1056)
    return LiveInvokeHandle(
        _outcome(RawOutcomeKind.SUCCESS, source_id=source_id, usage=tokens), (json.dumps(body).encode(),),
    )


async def _call(service, arguments, *, after_launch=None, raw=None, turns=1):
    """Post one call in the first of ``turns`` turns; return its answer and each turn's offered models."""

    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    offered = []
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="vibey:image", turn_id="turn-image")
        offered.append([model["model_id"] for model in hop["hosted_tools"].get(IMAGE, {}).get("models", [])])
        if after_launch is not None:
            after_launch()
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f'{hop["base_url"]}/hosted-tools/{IMAGE}',
                headers={"Authorization": f'Bearer {hop["token"]}', **hop["request_headers"]},
                data=raw if raw is not None else json.dumps({"arguments": arguments}),
            ) as response:
                answer = response.status, await response.json(content_type=None)
        for number in range(1, turns):
            nxt = await router.resolve_hop("menu-alias", process_scope="vibey:image", turn_id=f"turn-image-{number}")
            offered.append([model["model_id"] for model in nxt["hosted_tools"].get(IMAGE, {}).get("models", [])])
        return answer, offered
    finally:
        await gateway.close()


async def test_an_image_call_runs_directly_on_the_named_models_source(tmp_path):
    """MH-HOSTED-GATEWAY-007: the model's fixed fields and the call's own, one buffered call, the agreed answer."""

    service = _vibey_service(tmp_path, _sources(), handles=[_served(PNG, PNG, size="1536x1024")])
    (status, answer), (offered,) = await _call(
        service, {"model": "gpt-image-2", "prompt": "a lighthouse", "size": "1536x1024", "n": 2},
    )
    assert status == 200, answer
    # The default first, then the rest by id.
    assert offered == ["gpt-image-2", "gemini-2.5-flash-image", "gemini-3-pro-image-preview"]
    assert service.adapter.invocations == [(OPENAI, "gpt-image-2", "vibey")]
    sent = service.adapter.requests[0]
    assert sent.protocol == "openai_images"
    assert dict(sent) == {
        "prompt": "a lighthouse", "size": "1536x1024", "n": 2, "output_format": "jpeg", "output_compression": 90,
    }
    assert answer == {
        "record": {"status": "completed", "sources": []},
        "execution": {
            "mode": "direct",
            "executor_origin": {"provider": "openai", "api": "openai_responses", "model": "gpt-image-2"},
            "executor_source_id": OPENAI,
            "executor_billing": "metered",
        },
        "images": [{"data": PNG}, {"data": PNG}],
        "generation": {"size": "1536x1024", "quality": "medium"},
    }
    assert _usage_of(service, OPENAI)
    assert latest_ok(service, IMAGE) == (OPENAI, "gpt-image-2", "direct")


async def test_a_ratio_and_reference_images_map_to_the_endpoints_fields(tmp_path):
    """MH-HOSTED-GATEWAY-007: a ratio goes as aspect_ratio; reference images go as data URLs and make an edit."""

    service = _vibey_service(tmp_path, _sources(), handles=[_served(PNG, source_id=GEMINI), _served(PNG)])
    (status, _answer), _ = await _call(service, {"model": "gemini-2.5-flash-image", "prompt": "a cat", "size": "16:9"})
    assert status == 200
    assert dict(service.adapter.requests[0]) == {"prompt": "a cat", "aspect_ratio": "16:9", "response_format": "b64_json"}
    (status, _answer), _ = await _call(service, {"model": "gpt-image-2", "prompt": "make it blue", "images": [PNG]})
    assert status == 200
    assert service.adapter.requests[1]["images"] == [{"image_url": f"data:image/png;base64,{PNG}"}]


@pytest.mark.parametrize("case", ["model_outside_the_enum", "tool_turned_off", "turned_on_mid_turn"])
async def test_an_image_call_its_turn_cannot_make_is_refused_unrun(tmp_path, case):
    """MH-HOSTED-GATEWAY-007: the frozen enum authorizes every call; nothing outside it runs or is billed."""

    service = _vibey_service(tmp_path, _sources())
    if case != "model_outside_the_enum":
        service.store.config.hosted_tools[IMAGE] = ModelHubHostedToolConfig(enabled=False)

    def turned_on():
        if case == "turned_on_mid_turn":
            service.store.config.hosted_tools[IMAGE] = ModelHubHostedToolConfig(enabled=True)

    model = "dall-e-3" if case == "model_outside_the_enum" else "gpt-image-2"
    (status, answer), _ = await _call(service, {"model": model, "prompt": "a cat"}, after_launch=turned_on)
    assert (status, answer["error"]["code"]) == (409, "tool_not_enabled")
    assert service.adapter.invocations == []


@pytest.mark.parametrize(("arguments", "error"), [
    # Gemini takes no reference images: the record names the models that do.
    ({"model": "gemini-2.5-flash-image", "prompt": "edit", "images": [PNG]},
     "gemini-2.5-flash-image does not take images. Models that take it: gpt-image-2."),
    # GPT Image takes sizes in pixels only.
    ({"model": "gpt-image-2", "prompt": "wide", "size": "16:9"},
     "gpt-image-2 does not take size. Models that take it: gemini-2.5-flash-image, gemini-3-pro-image-preview."),
])
async def test_an_argument_the_model_does_not_take_fails_before_anything_runs(tmp_path, arguments, error):
    """MH-HOSTED-GATEWAY-007: never silently dropped, so an edit never becomes an unrelated generation."""

    service = _vibey_service(tmp_path, _sources())
    (status, answer), _ = await _call(service, arguments)
    assert status == 200
    assert answer == {"record": {"status": "failed", "error": error, "sources": [], "error_code": "image_param_unsupported"}}
    assert service.adapter.invocations == []


async def test_a_model_gone_mid_turn_is_unavailable_and_the_record_names_the_others(tmp_path):
    """MH-HOSTED-GATEWAY-007: admitted on the final snapshot; no fallback, but the alternatives are named."""

    service = _vibey_service(tmp_path, _sources())

    def removed():
        service.store.config.sources = [item for item in service.store.config.sources if item.id != GEMINI]

    (status, answer), _ = await _call(
        service, {"model": "gemini-3-pro-image-preview", "prompt": "a cat"}, after_launch=removed,
    )
    assert status == 200
    assert answer["record"]["error_code"] == "image_model_unavailable"
    assert answer["record"]["error"].endswith("Available: gpt-image-2, gemini-2.5-flash-image.")
    assert "execution" not in answer and service.adapter.invocations == []


NOT_AN_IMAGE = base64.b64encode(b"plain text, not an image").decode()
# Images read and the media sink do not take: a BMP, and an animated PNG whose acTL sits past any sniffed prefix.
BMP = base64.b64encode(struct.pack("<2sIHHIIiiHH", b"BM", 58, 0, 0, 54, 40, 1, 1, 1, 24) + bytes(28)).decode()
LATE_APNG = base64.b64encode(_png(animated=True, padding=10_000)).decode()


@pytest.mark.parametrize(("answer_body", "error"), [
    ({"created": 1, "data": [{"url": "https://cdn.example/image.png"}]}, "returned no image data"),
    ({"created": 1, "data": []}, "returned no image data"),
    ({"created": 1, "data": [{"b64_json": "not base64!"}]}, "could not be used"),
    ({"created": 1, "data": [{"b64_json": NOT_AN_IMAGE}]}, "could not be used"),
    ({"created": 1, "data": [{"b64_json": BMP}]}, "could not be used"),
    ({"created": 1, "data": [{"b64_json": LATE_APNG}]}, "could not be used"),
    ({"created": 1, "data": [{"b64_json": PNG}, {"b64_json": PNG}], "oversized": True}, "larger than allowed"),
    # A call asks for at most four images: an answer with more is not kept at all.
    ({"created": 1, "data": [{"b64_json": PNG}] * 5}, "larger than allowed"),
])
async def test_only_usable_images_complete_the_call(tmp_path, monkeypatch, answer_body, error):
    """MH-HOSTED-GATEWAY-007: completion is an allow-list; a URL is never fetched; the call is still metered."""

    if answer_body.pop("oversized", False):
        monkeypatch.setattr(images_module, "IMAGES_TOTAL_MAX_BYTES", len(base64.b64decode(PNG)) + 1)
    handle = LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=OPENAI, usage=ProtocolUsageReport.of(
        input_tokens=20, cached_input_tokens=0, output_tokens=10,
    )), (json.dumps(answer_body).encode(),))
    service = _vibey_service(tmp_path, _sources(), handles=[handle])
    (status, answer), _ = await _call(service, {"model": "gpt-image-2", "prompt": "a cat"})
    assert status == 200
    assert answer["record"]["status"] == "failed" and error in answer["record"]["error"]
    # The gateway's own reason carries the code each surface shows it by (#2589); the other models follow in its text.
    assert answer["record"]["error_code"] == {
        "returned no image data": "provider_no_image", "could not be used": "provider_image_unusable",
        "larger than allowed": "provider_too_large",
    }[error]
    assert "images" not in answer and answer["execution"]["executor_source_id"] == OPENAI
    assert _usage_of(service, OPENAI)
    assert latest_ok(service, IMAGE) is None


@pytest.mark.parametrize(("outcome", "refused"), [
    # The endpoint itself is not served: every image model the Source registers there.
    (_outcome(RawOutcomeKind.HTTP_ERROR, status=404, code="not_found_error", source_id=GEMINI),
     {"gemini-2.5-flash-image", "gemini-3-pro-image-preview"}),
    # A refusal naming the model: that model only.
    (_outcome(RawOutcomeKind.HTTP_ERROR, status=400, code="model_not_found", source_id=GEMINI),
     {"gemini-3-pro-image-preview"}),
    # Anything else teaches nothing: a request error, the provider's failure, the engine's own refusal.
    (_outcome(RawOutcomeKind.HTTP_ERROR, status=400, code="invalid_request_error", source_id=GEMINI), set()),
    (_outcome(RawOutcomeKind.HTTP_ERROR, status=503, code="server_error", source_id=GEMINI), set()),
    (_outcome(RawOutcomeKind.NETWORK_ERROR, code="engine_down", source_id=GEMINI), set()),
], ids=["endpoint-not-served", "model-refused", "request-error", "server-error", "engine-refused"])
async def test_only_an_upstream_refusal_of_the_model_or_endpoint_is_learned(tmp_path, outcome, refused):
    """MH-HOSTED-GATEWAY-007: the image narrow rule (§3.2); the next turn's enum drops what was refused."""

    service = _vibey_service(tmp_path, _sources(), handles=[InvokeHandle(outcome)])
    (status, answer), offered = await _call(service, {"model": "gemini-3-pro-image-preview", "prompt": "a cat"}, turns=2)
    assert status == 200 and answer["record"]["status"] == "failed"
    gemini = next(item for item in service.store.config.sources if item.id == GEMINI)
    learned = {
        model for model in ("gemini-2.5-flash-image", "gemini-3-pro-image-preview")
        if service.hosted_evidence().unsupported(gemini, model, FEATURE)
    }
    assert learned == refused
    assert offered[1] == [model for model in offered[0] if model not in refused]


@pytest.mark.parametrize("arguments", [
    {"model": "gpt-image-2", "prompt": "a cat", "style": "vivid"},
    {"model": "gpt-image-2", "prompt": " "},
    {"model": "gpt-image-2", "prompt": "a cat", "n": 5},
    {"model": "gpt-image-2", "prompt": "a cat", "size": "large"},
    {"model": "gpt-image-2", "prompt": "a cat", "images": [PNG] * 5},
    {"model": "gpt-image-2", "prompt": "a cat", "images": ["not base64!"]},
    {"model": "gpt-image-2", "prompt": "a cat", "images": [NOT_AN_IMAGE]},
    {"model": "gpt-image-2", "prompt": "a cat", "images": [BMP]},
])
async def test_a_malformed_image_call_is_refused_before_anything_runs(tmp_path, arguments):
    """MH-HOSTED-GATEWAY-007: Vibey checks the same before calling; the gateway is the execution boundary."""

    service = _vibey_service(tmp_path, _sources())
    (status, answer), _ = await _call(service, arguments)
    assert (status, answer["error"]["code"]) == (400, "invalid_request_error")
    assert service.adapter.invocations == []


async def test_an_image_call_body_is_bounded_by_its_reference_images(tmp_path, monkeypatch):
    """MH-HOSTED-GATEWAY-007: four reference images at read's inline limit fit; more is refused before parsing."""

    service = _vibey_service(tmp_path, _sources())
    padded = json.dumps({"arguments": {"model": "gpt-image-2", "prompt": "x" * images_module.IMAGE_CALL_MAX_BYTES}})
    (status, answer), _ = await _call(service, None, raw=padded.encode())
    assert (status, answer["error"]["code"]) == (400, "invalid_request_error")
    assert images_module.IMAGE_CALL_MAX_BYTES > 4 * (images_module.MAX_INLINE_IMAGE_BYTES * 4 // 3)


PRICED = ImageModelSpec(
    "gpt-image-2", "GPT Image 2", ("n", "size"), ("pixels",), kinds=("api_key",),
    prices=(
        ImageModelPrice(HostedToolPrice("image", 40, None, "https://example.test/pricing"), size="1024x1024"),
        ImageModelPrice(HostedToolPrice("image", 70, None, "https://example.test/pricing"), size="1536x1024"),
    ),
)


@pytest.mark.parametrize(("size", "cost"), [
    ("1536x1024", {"basis": "list", "usd": 0.14}),
    # No price for the size it delivered, and no token price held: unpriced, never zero.
    ("1024x1536", None),
])
async def test_an_image_call_costs_its_delivered_images_at_their_list_price(tmp_path, monkeypatch, size, cost):
    """MH-HOSTED-GATEWAY-007: the price that applies to what was delivered, times the images delivered."""

    monkeypatch.setattr(
        hosted_tools_module, "provider_image_models", lambda vendor: (PRICED,) if vendor == "openai" else (),
    )
    service = _vibey_service(tmp_path, [replace(_sources()[0])], handles=[_served(PNG, PNG, size=size)])
    (status, answer), _ = await _call(service, {"model": "gpt-image-2", "prompt": "a cat", "n": 2})
    assert status == 200 and answer["record"]["status"] == "completed"
    assert answer["execution"].get("cost") == cost


@pytest.mark.parametrize("status", [200, 502], ids=["served", "refused"])
async def test_an_image_answer_is_bounded_while_it_downloads(monkeypatch, status):
    """MH-HOSTED-GATEWAY-007: a relay that never stops is cut at the bound, before it reaches the disk spool."""

    from aiohttp import web

    from tests.test_model_hub_retry_advice import _engine, _source as _engine_source
    from vibe.model_hub_runtime import client as client_module

    monkeypatch.setitem(client_module._BUFFERED_RESPONSE_MAX_BYTES, "openai_images", 256 * 1024)
    sent = []

    async def respond(request):
        response = web.StreamResponse(status=status, headers={"Content-Type": "application/json"})
        await response.prepare(request)
        try:
            for _ in range(64):
                chunk = b"x" * (64 * 1024)
                await response.write(chunk)
                sent.append(len(chunk))
        except (ConnectionResetError, RuntimeError):
            pass
        return response

    async with _engine(respond) as (client, requests):
        handle = await client.invoke(
            _engine_source("openai_responses"), "model-a", {"prompt": "a cat"}, stream=False,
            request_protocol="openai_images",
        )
        outcome = await handle.outcome()
        await handle.close_stream()
    assert requests[0][0] == "/v1/images/generations"
    assert (outcome.kind, outcome.error_code, outcome.http_status) == (
        RawOutcomeKind.NETWORK_ERROR, "response_too_large", status,
    )
    assert handle.stream is None
