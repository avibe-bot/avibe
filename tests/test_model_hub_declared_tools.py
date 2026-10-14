"""MH-HOSTED-GATEWAY-001: the engine forwards declared tools unchanged, for every caller.

The engine used to add ``{"type": "image_generation", "output_format": "png"}``
to every request its Codex executor served, for any caller (CLIProxyAPI
``ensureImageGenerationTool`` at d318bcc3). It runs with
``disable-image-generation: passthrough``, and nothing adds the tool back:
Codex CLI from 0.144.0 drops a hosted ``image_generation_call`` instead of
reporting it, so an added tool only billed images nobody saw. Each case
drives the real gateway and engine adapter against a loopback engine and
checks the tools it receives.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web

from core.handlers.model_hub.capability_evidence import EvidenceFeature, EvidenceRecord, identity_digest
from core.handlers.model_hub.hosted_tools import HOSTED_PROBE_SHAPE, engine_stamp
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from modules.agents.model_hub import ModelHubRuntimeRouter
from tests.test_model_hub_l3 import _canonicalize_fixed_test_routes, _service, _source
from tests.test_model_hub_vibey_consumer import _vibey_service
from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter
from vibe.model_hub_runtime.client import EngineClient, EngineConnection
from vibe.model_hub_runtime.state import EngineStateStore

SHELL = {"type": "function", "name": "shell", "parameters": {"type": "object", "properties": {}}}
DECLARED_IMAGE = {"type": "image_generation", "quality": "high"}
INPUT = [{"role": "user", "content": [{"type": "input_text", "text": "画一只猫 café"}]}]
COMPLETED = {
    "/v1/responses": (
        b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"ok"}\n\n'
        b'event: response.completed\ndata: {"type":"response.completed","response":'
        b'{"id":"resp_1","status":"completed","usage":{"input_tokens":2,"output_tokens":1}}}\n\n'
    ),
    "/v1/messages": (
        b'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","usage":'
        b'{"input_tokens":2,"output_tokens":0}}}\n\n'
        b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
        b'"delta":{"type":"text_delta","text":"ok"}}\n\n'
        b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        b'"usage":{"output_tokens":1}}\n\n'
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    ),
}


@asynccontextmanager
async def _engine(received: list[bytes]):
    async def respond(request: web.Request) -> web.Response:
        received.append(await request.read())
        return web.Response(body=COMPLETED[request.path], content_type="text/event-stream")

    app = web.Application()
    app.router.add_post("/{path:.*}", respond)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    try:
        yield f"http://127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()


def _wire(service, tmp_path, root: str, grants: dict[str, dict]) -> EngineStateStore:
    """Bind every configured Source in a real engine state store and adapter at ``root``."""

    state = EngineStateStore(tmp_path / "engine")
    for configured in service.store.config.sources:
        if configured.id in grants:
            configured.credential_ref = state.bind_oauth_credential(configured.id, configured.vendor, "grant.json")
            state.write_oauth_auth_file("grant.json", {
                "type": "codex",
                "prefix": state.credential_metadata(configured.credential_ref)["prefix"],
                "access_token": "private-access-fixture",
                **grants[configured.id],
            })
        else:
            configured.credential_ref = state.store_api_key(
                "fixture-key", vendor=configured.vendor, protocol=configured.protocol,
            )
    state.sync_sources(service._bindings(service.store.load()))
    client = EngineClient(EngineConnection(root, "fixture-management", "fixture-gateway"))
    adapter = CLIProxyEngineAdapter(supervisor=SimpleNamespace(client=lambda: client), state_store=state)
    adapter.ensure_installed = service.adapter.ensure_installed
    service.adapter = adapter
    service._engine_synced = True
    return state


async def _post(url: str, token: str, body: dict, headers: dict | None = None) -> None:
    async with aiohttp.ClientSession() as downstream:
        async with downstream.post(
            url, json=body, headers={"Authorization": f"Bearer {token}", **(headers or {})},
        ) as response:
            assert response.status == 200, await response.text()
            await response.read()


def _subscription(source):
    return replace(source, kind="subscription", billing="monthly")


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [False, True], ids=["undeclared", "declared"])
@pytest.mark.parametrize(("backend", "path", "subscription"), [
    ("codex", "responses", False), ("codex", "responses", True), ("claude", "messages", False),
    ("opencode", "responses", False), ("vibey", "responses", False),
], ids=["codex_api_key", "codex_subscription", "claude", "opencode", "vibey"])
async def test_every_caller_reaches_the_engine_with_exactly_its_declared_tools(
    tmp_path, backend, path, subscription, declared,
):
    """MH-HOSTED-GATEWAY-001: no caller, Codex CLI included, gets an image tool it did not declare.

    A Vibey request that declares it is a native declaration, so it is pinned to
    a hop verified for the tool (MH-HOSTED-GATEWAY-002).
    """
    configured = _source("src_shared001", "Responses key", model_id="gpt-6-astra" if backend == "vibey" else "shared-model")
    if subscription:
        configured = _subscription(configured)
    tools = [SHELL, DECLARED_IMAGE] if declared else [SHELL]
    if backend == "claude":
        tools = [{"name": "shell", "input_schema": {"type": "object"}}, *([DECLARED_IMAGE] if declared else [])]
    received: list[bytes] = []
    async with _engine(received) as root:
        if backend == "vibey":
            service = _vibey_service(tmp_path, [configured])
        else:
            service = _service(tmp_path, sources=[configured])
            _canonicalize_fixed_test_routes(service)
            service.store.requested_models["opencode"] = "shared-model"
        # A ChatGPT subscription's grant on a paid plan: where the engine's skip conditions never applied.
        _wire(service, tmp_path, root, {configured.id: {"plan_type": "plus"}} if subscription else {})
        pin = {}
        if backend == "vibey" and declared:
            source = service.store.config.sources[0]
            service.capability_evidence.write(
                EvidenceRecord(
                    source_id=source.id, model_id="gpt-6-astra",
                    feature=EvidenceFeature.hosted("image_generation", "call"), status="verified", basis="probe",
                    observed_at=service.now(), engine=engine_stamp(), shape=HOSTED_PROBE_SHAPE,
                    source_identity=identity_digest(source),
                ),
                live=service.store.config.sources,
            )
            pin = {"x-avibe-pin-source": source.id}
        gateway = ModelHubTurnGateway(service)
        try:
            if backend == "vibey":
                router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
                hop = await router.resolve_hop("menu-alias", process_scope="vibey:image", turn_id="turn-image")
                url, token, model = f'{hop["base_url"]}/{path}', hop["token"], hop["runtime_model"]
                pin = {**hop["request_headers"], **(pin or {})}
            else:
                base_url, token = await gateway.endpoint(
                    backend, process_scope="/repo", turn_id="turn_image_tool",
                    requested_model_id=service.store.requested_models[backend],
                    resolved_model_id="shared-model", source_id=configured.id,
                )
                url, model = f"{base_url}/v1/{path}", "shared-model"
            await _post(url, token, {"model": model, "stream": True, "tools": tools, "input": INPUT}, pin)
        finally:
            await gateway.close()
    assert len(received) == 1
    assert json.loads(received[0])["tools"] == tools
