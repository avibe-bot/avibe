"""MH-HOSTED-GATEWAY-001, once per release (``vibey-hosted-tools.md`` §7.1): no image tool for Codex through the Hub.

The real Codex CLI sends a turn through the production gateway and the pinned
engine, which runs with ``disable-image-generation: passthrough``, to a
loopback upstream. The upstream must see no image tool: the engine no longer
adds one, and neither does the gateway. A Vibey request to the same Source must
see none either.

What Codex does with an image is not asserted. From Codex CLI 0.144.0
(openai/codex#31596) a hosted ``image_generation_call`` in a response stream is
not reported as an ``imageGeneration`` item, so Avibe's Codex backend never
receives one to persist; that is why nothing adds the tool for Codex.

Run it with ``-m e2e_model_hub``, ``VIBE_MODEL_HUB_ENGINE_MANIFEST_PATH`` set to
an offline manifest of the engine under test, and ``codex`` on ``PATH``.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

from config.v2_config import ModelHubBackendModelConfig, model_hub_fixed_menu_ids
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from modules.agents.codex.transport import CodexTransport
from modules.agents.model_hub import ModelHubRuntimeRouter, build_codex_hub_launch
from tests.e2e.test_model_hub_catalog_consumer import (
    codex_catalog_runtime,  # noqa: F401 -- fixture dependency
    rejected_external_proxy,  # noqa: F401 -- fixture dependency
)
from tests.e2e.test_model_hub_runtime import _isolated_engine_adapter
from tests.scenario_harness.model_hub import MemoryModelHubStore, service_for
from tests.test_model_hub_google import _server
from tests.test_model_hub_l3 import _source
from tests.test_model_hub_vibey_consumer import _vibey_service
from vibe.backend_model_catalog import _codex_hub_catalog_bytes


pytestmark = pytest.mark.e2e_model_hub

MODEL = "fixture-image-model"


def _event(payload: dict) -> bytes:
    return f"event: {payload['type']}\ndata: {json.dumps(payload)}\n\n".encode()


def _answer() -> list[bytes]:
    message = {"id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed",
               "content": [{"type": "output_text", "text": "I can't draw here.", "annotations": []}]}
    events = [
        {"type": "response.created", "response": {
            "id": "resp_fixture", "object": "response", "status": "in_progress", "model": MODEL, "output": []}},
        {"type": "response.output_item.added", "output_index": 0,
         "item": {**message, "status": "in_progress", "content": []}},
        {"type": "response.output_text.delta", "output_index": 0, "item_id": "msg_fixture", "content_index": 0,
         "delta": "I can't draw here."},
        {"type": "response.output_item.done", "output_index": 0, "item": message},
        {"type": "response.completed", "response": {
            "id": "resp_fixture", "object": "response", "status": "completed", "model": MODEL, "output": [message],
            "usage": {"input_tokens": 12, "output_tokens": 6, "total_tokens": 18}}},
    ]
    return [_event({**event, "sequence_number": index}) for index, event in enumerate(events)]


def _image_tools(body: dict) -> list:
    tools = body.get("tools") if isinstance(body, dict) else None
    return [tool for tool in tools or () if isinstance(tool, dict) and tool.get("type") == "image_generation"]


@asynccontextmanager
async def _through_engine(tmp_path, monkeypatch):
    """The production gateway and the pinned engine in front of a recording upstream."""

    requests: list[dict] = []

    async def respond(request):
        requests.append({"path": request.path, "body": await request.json()})
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for frame in _answer():
            await response.write(frame)
        await response.write_eof()
        return response

    with _isolated_engine_adapter(tmp_path / "engine", monkeypatch) as adapter:
        async with AsyncExitStack() as stack:
            root = await stack.enter_async_context(_server(respond))
            # An OpenAI-protocol API key: the engine serves it through its Codex executor,
            # where it used to add its image tool.
            source = _source("src_codeximage1", "Image fixture", vendor="custom", protocol="openai_responses",
                             model_id=MODEL)
            source.base_url = root + "/v1"
            source.credential_ref = adapter.state_store.store_api_key(
                "sk-synthetic-codex-image", vendor="custom", protocol="openai_responses", base_url=source.base_url,
            )
            (tmp_path / "service").mkdir()
            config = _vibey_service(tmp_path / "service", [source]).store.config
            service = service_for(tmp_path / "service", MemoryModelHubStore(config), adapter)
            await service.runtime_start()
            gateway = ModelHubTurnGateway(service)
            try:
                yield ModelHubRuntimeRouter(service=service, turn_gateway=gateway), requests
            finally:
                await gateway.close()


async def _codex_turn(codex_runtime, launch) -> None:
    """One real Codex CLI turn asking for a picture, through the launch Avibe issued."""

    binary, runtime, raw_catalog = codex_runtime
    Path(runtime.env["CODEX_HOME"], "config.toml").write_text('cli_auth_credentials_store = "file"\n')
    catalog_path = runtime.home / "image-models.json"
    catalog_path.write_bytes(_codex_hub_catalog_bytes(
        raw_catalog, [ModelHubBackendModelConfig(id=launch.runtime_model, context_window=128_000).to_payload()],
    ))
    args, env = build_codex_hub_launch([], runtime.env, launch, model_catalog_path=catalog_path)
    transport = CodexTransport(binary=binary, cwd=str(runtime.home), runtime_args=args, runtime_env=env)
    completed: asyncio.Queue = asyncio.Queue()

    async def notify(method, params):
        if method == "turn/completed":
            await completed.put(params)

    transport.on_notification(notify)
    await transport.start()
    try:
        thread = await transport.send_request(
            "thread/start", {"model": launch.runtime_model, "cwd": str(runtime.home), "approvalPolicy": "never"},
        )
        await transport.send_request("turn/start", {
            "threadId": thread["thread"]["id"],
            "input": [{"type": "text", "text": "Draw a small red square."}],
            # As Avibe's Codex backend sends it: the route and turn this request belongs to.
            "responsesapiClientMetadata": dict(launch.gateway_request_metadata),
        })
        finished = await asyncio.wait_for(completed.get(), timeout=60)
        assert finished["turn"]["status"] == "completed", finished
    finally:
        await transport.stop()


@pytest.mark.asyncio
async def test_codex_through_the_hub_gets_no_image_tool_from_a_passthrough_engine(
    tmp_path, monkeypatch, codex_catalog_runtime,
):
    """MH-HOSTED-GATEWAY-001: a Codex turn and a Vibey request reach the upstream with no image tool they did not declare."""

    async with _through_engine(tmp_path, monkeypatch) as (router, requests):
        _binary, runtime, _catalog = codex_catalog_runtime
        launch = await router.resolve(
            "codex", model_hub_fixed_menu_ids("codex")[0], process_scope=str(runtime.home), turn_id="turn-codex-image",
        )
        await _codex_turn(codex_catalog_runtime, launch)
        codex_requests = list(requests)

        hop = await router.resolve_hop("menu-alias", process_scope="vibey:image", turn_id="turn-vibey-image")
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f'{hop["base_url"]}/responses',
                headers={"Authorization": f'Bearer {hop["token"]}', **hop["request_headers"]},
                json={"model": hop["runtime_model"], "stream": True, "input": "Say hello."},
            ) as response:
                assert response.status == 200, await response.text()
                await response.read()
        vibey_requests = requests[len(codex_requests):]

    # Codex declares its own tools, none of them a hosted image tool, and nothing adds one on the way.
    assert codex_requests
    assert all(_image_tools(request["body"]) == [] for request in codex_requests), [
        request["body"].get("tools") for request in codex_requests
    ]
    assert len(vibey_requests) == 1 and _image_tools(vibey_requests[0]["body"]) == []
