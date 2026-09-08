"""Save, inventory and explicitly selected model tests have independent results."""

import asyncio
import copy
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from core.handlers.model_hub.adapter import DiscoveredModel, RawOutcomeKind, SOURCE_PROTOCOLS, SourceBinding
from core.handlers.model_hub.service import ModelHubError
from tests.test_model_hub_api import FakeAdapter, FakeInvokeHandle, _service
from tests.test_model_hub_unverified import _draft


@pytest.mark.parametrize("protocol", SOURCE_PROTOCOLS)
@pytest.mark.parametrize("listing", ["success", "empty", "invalid", "failed"])
def test_save_inventory_is_ordered_optional_and_never_authenticates(tmp_path, protocol, listing):
    service, store, adapter = _service(tmp_path)
    rows = {
        "success": (DiscoveredModel("z-model"), DiscoveredModel("a-model")),
        "empty": (),
        "invalid": (DiscoveredModel("duplicate"), DiscoveredModel("duplicate")),
        "failed": (),
    }[listing]
    adapter.discover_models = AsyncMock(
        return_value=rows,
        side_effect=ModelHubError("discovery_failed") if listing == "failed" else None,
    )
    adapter.observe_source = AsyncMock(side_effect=AssertionError("observation is not admission"))
    result = asyncio.run(service.create_source(_draft(protocol=protocol)))["source"]
    assert result["protocol"] == protocol
    assert result["verification_pending"]
    assert [model["id"] for model in result["models"]] == (
        ["z-model", "a-model"] if listing == "success" else []
    )
    assert len(store.config.sources) == 1
    adapter.discover_models.assert_awaited_once()
    adapter.observe_source.assert_not_awaited()


@pytest.mark.parametrize("protocol", SOURCE_PROTOCOLS)
@pytest.mark.parametrize("kind", list(RawOutcomeKind))
def test_probe_invokes_selected_source_even_without_agent_routes_and_isolates_failure(tmp_path, protocol, kind):
    service, store, adapter = _service(tmp_path)

    async def scenario():
        result = (await service.create_source(_draft(protocol=protocol)))["source"]
        for agent in store.config.agents.values():
            agent.mode = "direct"
            agent.sources.order = []
        source_id = result["id"]
        model_id = result["models"][0]["id"]
        before = copy.deepcopy(store.config.to_payload())
        original = adapter.invoke

        async def invoke(*args, **kwargs):
            handle = await original(*args, **kwargs)
            return FakeInvokeHandle(replace(
                await handle.outcome(), kind=kind,
                http_status=200 if kind is RawOutcomeKind.SUCCESS else 401,
                error_code=None if kind is RawOutcomeKind.SUCCESS else "invalid_api_key",
            ))

        adapter.invoke = AsyncMock(side_effect=invoke)
        adapter.discover_models = AsyncMock(side_effect=AssertionError("no discovery during a test"))
        answer = await service.probe_source(source_id, {"model": model_id})
        assert answer["source_id"] == source_id
        assert answer["model_id"] == model_id
        assert answer["protocol"] == protocol
        assert answer["reachable"] is (kind is RawOutcomeKind.SUCCESS)
        assert (answer["error"] is None) is answer["reachable"]
        adapter.invoke.assert_awaited_once()
        args = adapter.invoke.call_args.args
        assert args[:2] == (source_id, model_id)
        assert args[2].protocol == protocol
        assert args[2]["model"] == model_id
        budget_key = "max_output_tokens" if protocol == "openai_responses" else "max_tokens"
        assert args[2][budget_key] == 128
        assert args[3:] == (False, "opencode")
        adapter.discover_models.assert_not_awaited()
        after = store.config.to_payload()
        if answer["reachable"]:
            assert not store.config.sources[0].verification_pending
            before["sources"][0].pop("verification_pending", None)
            after["sources"][0].pop("verification_pending", None)
        assert before == after

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["delete", "replace", "same_handle"])
def test_test_settlement_cannot_verify_a_different_credential_identity(tmp_path, change):
    class ConcurrentAdapter(FakeAdapter):
        async def invoke(self, *args, **kwargs):
            handle = await super().invoke(*args, **kwargs)
            if change == "delete":
                store.config.sources.clear()
            else:
                current = store.config.sources[0]
                if change == "replace":
                    current.credential_ref = "cred_replacement"
                service._mark_source_unverified(current)
            return handle

    service, store, _ = _service(tmp_path, ConcurrentAdapter())

    async def scenario():
        source = (await service.create_source(_draft()))["source"]
        answer = await service.probe_source(source["id"], {"model": source["models"][0]["id"]})
        assert answer["reachable"]
        if change == "delete":
            assert not store.config.sources
        else:
            assert store.config.sources[0].verification_pending

    asyncio.run(scenario())


@pytest.mark.parametrize("payload", [{}, {"model": None}, {"model": "missing"}, {"model": "x", "key": "ignored"}])
def test_probe_refuses_invalid_or_unknown_selection_before_runtime_work(tmp_path, payload):
    service, _, adapter = _service(tmp_path)
    source = asyncio.run(service.create_source(_draft()))["source"]
    adapter.invoke = AsyncMock()
    before = adapter.start_calls
    with pytest.raises(ModelHubError):
        asyncio.run(service.probe_source(source["id"], payload))
    assert adapter.start_calls == before
    adapter.invoke.assert_not_awaited()


def test_manual_model_addition_never_calls_upstream_discovery(tmp_path):
    service, store, adapter = _service(tmp_path)
    source = asyncio.run(service.create_source(_draft()))["source"]
    adapter.discover_models = AsyncMock(side_effect=AssertionError("manual means local inventory"))
    adapter.observe_source = AsyncMock(side_effect=AssertionError("manual means no detection"))
    before = copy.deepcopy(store.config.sources[0].models)
    result = asyncio.run(service.add_custom_model(source["id"], {
        "model_id": "manual-example", "reasoning_efforts": [],
    }))
    assert result["models"][-1]["id"] == "manual-example"
    assert store.config.sources[0].models[:-1] == before
    adapter.discover_models.assert_not_awaited()
    adapter.observe_source.assert_not_awaited()


@pytest.mark.parametrize("listing", ["empty", "invalid", "timeout"])
def test_optional_discovery_preserves_manual_inventory(tmp_path, listing):
    service, _, adapter = _service(tmp_path)
    adapter.discover_models = AsyncMock(
        return_value=(DiscoveredModel("duplicate"),) * 2 if listing == "invalid" else (),
        side_effect=TimeoutError if listing == "timeout" else None,
    )
    draft = _draft()
    draft["models"] = [{"id": "manual-model", "origin": "manual", "reasoning_efforts": []}]
    source = asyncio.run(service.create_source(draft))["source"]
    assert [(model["id"], model["origin"]) for model in source["models"]] == [("manual-model", "manual")]
    assert source["verification_pending"]


@pytest.mark.parametrize("protocol", SOURCE_PROTOCOLS)
def test_source_probe_crosses_real_adapter_and_http_transport(tmp_path, protocol):
    from aiohttp import web

    from core.handlers.model_hub.rpc import dispatch_model_hub_rpc
    from tests.test_model_hub_transport_leases import LeaseSupervisor
    from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter
    from vibe.model_hub_runtime.state import EngineStateStore

    async def scenario():
        requests = []

        async def handler(request):
            body = await request.json()
            requests.append((request.path, body))
            return web.json_response({
                "choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}],
                "content": [{"type": "text", "text": "pong"}],
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "pong"}]}],
                "usage": {"input_tokens": 2, "output_tokens": 1, "prompt_tokens": 2, "completion_tokens": 1},
            })

        app = web.Application()
        app.router.add_post("/{path:.*}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        try:
            origin = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
            service, store, adapter = _service(tmp_path)
            source = (await service.create_source(_draft(protocol=protocol)))["source"]
            model_id = source["models"][0]["id"]
            runtime_store = EngineStateStore(tmp_path / "engine")
            ref = runtime_store.store_api_key("fixture-key", protocol=protocol, base_url=source["base_url"])
            binding = SourceBinding(
                source_id=source["id"], vendor="custom", protocol=protocol,
                base_url=source["base_url"], credential_ref=ref, allowed_origins=(), model_ids=(model_id,),
            )
            runtime_store.sync_sources([binding])
            transport = CLIProxyEngineAdapter(supervisor=LeaseSupervisor(origin), state_store=runtime_store)
            adapter.invoke = transport.invoke
            service._meter_call = AsyncMock()
            answer = await dispatch_model_hub_rpc(service, "probe_source", {
                "source_id": source["id"], "probe": {"model": model_id},
            })
            assert answer["reachable"]
            assert len(requests) == 1
            path, body = requests[0]
            assert path == {"anthropic": "/v1/messages", "openai_chat": "/v1/chat/completions", "openai_responses": "/v1/responses"}[protocol]
            assert body["model"] == f"{runtime_store.get_source(source['id']).prefix}/{model_id}"
            assert body["stream"] is False
            assert transport._active_transports == 0
            assert not store.config.sources[0].verification_pending
            service._meter_call.assert_awaited_once()
            assert service._meter_call.call_args.kwargs["outcome"].usage is not None
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


@pytest.mark.parametrize("termination", ["cancel", "timeout"])
def test_probe_termination_closes_handle_and_releases_mutation_owner(tmp_path, monkeypatch, termination):
    service, store, adapter = _service(tmp_path)

    async def scenario():
        source = (await service.create_source(_draft()))["source"]
        entered = asyncio.Event()
        original = adapter.invoke
        handle_closed = AsyncMock()

        async def invoke(*args, **kwargs):
            handle = await original(*args, **kwargs)

            async def pending_outcome():
                entered.set()
                await asyncio.Event().wait()

            handle.outcome = pending_outcome
            handle.close_stream = handle_closed
            return handle

        adapter.invoke = invoke
        service._meter_call = AsyncMock()
        if termination == "timeout":
            original_timeout = asyncio.timeout
            monkeypatch.setattr(asyncio, "timeout", lambda _seconds: original_timeout(0.02))
        task = asyncio.create_task(service.probe_source(source["id"], {"model": source["models"][0]["id"]}))
        await entered.wait()
        if termination == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            answer = await task
            assert not answer["reachable"]
            assert answer["error"] == "models.source.cooldown.timeout"
        assert not service._mutation_lock.locked()
        assert store.config.sources[0].verification_pending
        handle_closed.assert_awaited_once()
        service._meter_call.assert_awaited_once()

    asyncio.run(scenario())
