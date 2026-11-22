"""A speculative failure must leave the same Source available for immediate plain replay."""

from __future__ import annotations

import aiohttp
import pytest

from core.handlers.model_hub.adapter import RawOutcomeKind
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from modules.agents.model_hub import ModelHubRuntimeRouter
from tests.test_model_hub_l3 import InvokeHandle, LiveInvokeHandle, _outcome, _source
from tests.test_model_hub_vibey_consumer import _vibey_service


@pytest.mark.asyncio
async def test_pinned_thread_failure_does_not_cool_the_replay_source(tmp_path):
    primary = _source("src_primary01", "Claude", vendor="anthropic", protocol="anthropic", model_id="claude-test")
    svc = _vibey_service(
        tmp_path,
        [primary],
        handles=[
            InvokeHandle(_outcome(RawOutcomeKind.HTTP_ERROR, status=429, code="rate_limit_error")),
            LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=primary.id), (b"{}",)),
        ],
    )
    gateway = ModelHubTurnGateway(svc)
    router = ModelHubRuntimeRouter(service=svc, turn_gateway=gateway)
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="vibey:thread", turn_id="turn-thread")
        body = {"model": hop["runtime_model"], "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}
        headers = {"x-api-key": hop["token"], **hop["request_headers"]}
        async with aiohttp.ClientSession() as client:
            async with client.post(
                hop["base_url"] + "/messages",
                json={**body, "thread": {"type": "create"}},
                headers={**headers, "x-avibe-pin-source": primary.id, "anthropic-beta": "message-threads-2026-08-12"},
            ) as r:
                assert r.status == 409
            assert svc.store.config.sources[0].state.status == "standby"
            async with client.post(hop["base_url"] + "/messages", json=body, headers=headers) as r:
                assert r.status == 200
        assert [hop[0] for hop in svc.adapter.invocations] == [primary.id, primary.id]
    finally:
        await gateway.close()
