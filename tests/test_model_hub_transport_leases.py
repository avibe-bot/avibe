from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from aiohttp import web

from core.handlers.model_hub.adapter import RawOutcomeKind, SourceBinding
from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter
from vibe.model_hub_runtime.client import EngineClient, EngineConnection
from vibe.model_hub_runtime.supervisor import EngineUnavailableError
from vibe.model_hub_runtime.state import EngineStateStore


FIRST = b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'
LAST = b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'


class LeaseSupervisor:
    def __init__(self, origin):
        self.connection = EngineConnection(origin, "fixture-management", "fixture-token")
        self.reload_started = threading.Event()
        self.allow_reload = threading.Event()
        self.allow_reload.set()
        self.reload_count = 0
        self.fail_reload = False

    def client(self):
        return EngineClient(self.connection)

    def client_if_running(self):
        return self.client()

    def restart_if_running(self):
        raise AssertionError("a source save must hot-reload, never restart")

    def reload_config_if_running(self, _previous=None):
        self.reload_count += 1
        self.reload_started.set()
        assert self.allow_reload.wait(3)
        if self.fail_reload:
            self.fail_reload = False
            raise EngineUnavailableError("models.engine.health_failed")


@asynccontextmanager
async def _transport(tmp_path):
    received = asyncio.Event()
    finish = asyncio.Event()
    requests = []

    async def handler(request):
        body = await request.json()
        requests.append(body)
        received.set()
        if not body["stream"]:
            await finish.wait()
            return web.json_response({"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]})
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(FIRST)
        await finish.wait()
        try:
            await response.write(LAST)
            await response.write_eof()
        except (ConnectionResetError, RuntimeError):
            pass
        return response

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    origin = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    store = EngineStateStore(tmp_path / "engine")
    ref = store.store_api_key("fixture-key", base_url="https://api.example/v1")
    binding = SourceBinding(
        source_id="src_lease0001",
        vendor="custom",
        protocol="openai_chat",
        base_url="https://api.example/v1",
        credential_ref=ref,
        allowed_origins=(),
        model_ids=("listed",),
        route_model_ids=("unknown",),
    )
    store.sync_sources([binding])
    supervisor = LeaseSupervisor(origin)
    adapter = CLIProxyEngineAdapter(supervisor=supervisor, state_store=store)
    try:
        yield adapter, supervisor, binding, received, finish, requests
    finally:
        finish.set()
        supervisor.allow_reload.set()
        await runner.cleanup()


@pytest.mark.parametrize("stream", [False, True])
def test_sync_applies_during_buffered_and_streaming_transport_without_waiting(tmp_path, stream):
    """A save must not wait for, restart under, or truncate an in-flight call."""

    async def run():
        async with _transport(tmp_path) as (adapter, supervisor, binding, received, finish, requests):
            invoke = asyncio.create_task(adapter.invoke(binding.source_id, "unknown", {}, stream, "opencode"))
            await asyncio.wait_for(received.wait(), 2)
            handle = await invoke if stream else None
            if handle is not None:
                assert await anext(handle.stream) == FIRST
            changed = replace(binding, route_model_ids=("unknown", "new"))
            await asyncio.wait_for(adapter.sync_sources([changed]), 2)
            assert supervisor.reload_count == 1
            assert adapter._active_transports == 1
            # The fixture holds every upstream response until ``finish``; the
            # new route is admitted while the first call is still open.
            later = asyncio.create_task(adapter.invoke(binding.source_id, "new", {}, False, "opencode"))
            async with asyncio.timeout(2):
                while len(requests) < 2:
                    await asyncio.sleep(0.01)
            finish.set()
            if handle is not None:
                assert b"".join([part async for part in handle.stream]) == LAST
            else:
                handle = await asyncio.wait_for(invoke, 2)
            later_handle = await asyncio.wait_for(later, 2)
            assert (await handle.outcome()).kind is RawOutcomeKind.SUCCESS
            assert (await later_handle.outcome()).kind is RawOutcomeKind.SUCCESS
            await handle.close_stream()
            await later_handle.close_stream()
            assert adapter._active_transports == 0
            assert requests[1]["model"].endswith("/new")

    asyncio.run(run())


def test_sync_keeps_an_admitted_request_routable_until_the_engine_has_read_it(tmp_path, monkeypatch):
    """A removal saved after admission must not strand a request not yet sent."""

    async def run():
        async with _transport(tmp_path) as (adapter, supervisor, binding, received, finish, requests):
            connect = asyncio.Event()
            original = EngineClient.invoke

            async def delayed_invoke(self, *args, **kwargs):
                await connect.wait()
                return await original(self, *args, **kwargs)

            monkeypatch.setattr(EngineClient, "invoke", delayed_invoke)
            invoke = asyncio.create_task(adapter.invoke(binding.source_id, "unknown", {}, False, "opencode"))
            async with asyncio.timeout(2):
                while adapter._requests_sent.is_set():
                    await asyncio.sleep(0.01)
            sync = asyncio.create_task(adapter.sync_sources([replace(binding, route_model_ids=())]))
            await asyncio.sleep(0.05)
            assert supervisor.reload_count == 0
            connect.set()
            await asyncio.wait_for(received.wait(), 2)
            await asyncio.wait_for(sync, 2)
            assert supervisor.reload_count == 1
            assert requests[0]["model"].endswith("/unknown")
            finish.set()
            handle = await asyncio.wait_for(invoke, 2)
            assert (await handle.outcome()).kind is RawOutcomeKind.SUCCESS
            await handle.close_stream()
            assert adapter._unsent_requests == 0 and adapter._active_transports == 0

    asyncio.run(run())


@pytest.mark.parametrize("fail_reload", [False, True])
def test_cancelled_reload_holds_admission_until_commit_or_rollback(tmp_path, fail_reload):
    async def run():
        async with _transport(tmp_path) as (adapter, supervisor, binding, _received, finish, requests):
            original = adapter.state_store.list_sources()
            supervisor.allow_reload.clear()
            supervisor.fail_reload = fail_reload
            sync = asyncio.create_task(adapter.sync_sources([replace(binding, route_model_ids=("changed",))]))
            assert await asyncio.to_thread(supervisor.reload_started.wait, 2)
            sync.cancel()
            await asyncio.sleep(0)
            assert not sync.done()
            assert adapter._routing_lock.locked()
            later = asyncio.create_task(adapter.invoke(binding.source_id, "listed", {}, False, "opencode"))
            await asyncio.sleep(0)
            assert requests == []
            supervisor.allow_reload.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(sync, 2)
            finish.set()
            handle = await asyncio.wait_for(later, 2)
            assert (await handle.outcome()).kind is RawOutcomeKind.SUCCESS
            await handle.close_stream()
            if fail_reload:
                assert adapter.state_store.list_sources() == original
                assert supervisor.reload_count == 2
            else:
                assert adapter.state_store.list_sources()[0].route_model_ids == ("changed",)
            assert adapter._active_transports == 0
            assert not adapter._routing_lock.locked()

    asyncio.run(run())


def test_two_concurrent_saves_are_serial_and_reload_failure_restores_projection(tmp_path):
    async def run():
        async with _transport(tmp_path) as (adapter, supervisor, binding, _received, _finish, _requests):
            original = adapter.state_store.list_sources()
            supervisor.fail_reload = True
            with pytest.raises(EngineUnavailableError):
                await adapter.sync_sources([replace(binding, route_model_ids=("bad",))])
            assert adapter.state_store.list_sources() == original
            assert supervisor.reload_count == 2
            supervisor.reload_started.clear()
            supervisor.allow_reload.clear()
            first = asyncio.create_task(adapter.sync_sources([replace(binding, route_model_ids=("first",))]))
            assert await asyncio.to_thread(supervisor.reload_started.wait, 2)
            second = asyncio.create_task(adapter.sync_sources([replace(binding, route_model_ids=("second",))]))
            await asyncio.sleep(0)
            assert adapter.state_store.list_sources()[0].route_model_ids == ("first",)
            supervisor.allow_reload.set()
            await asyncio.wait_for(asyncio.gather(first, second), 2)
            assert adapter.state_store.list_sources()[0].route_model_ids == ("second",)
            assert supervisor.reload_count == 4
            assert not adapter._routing_lock.locked()

    asyncio.run(run())


def test_service_save_completes_while_a_stream_is_active(tmp_path):
    """The reported hang: a model-list save during an active turn must settle."""
    from tests.test_model_hub_resolution import _service, _source
    from tests.test_model_hub_routing_modes import MODEL, _sparse_config

    async def run():
        async with _transport(tmp_path) as (adapter, supervisor, binding, _received, finish, _requests):
            source = _source(binding.source_id, (), vendor="custom", credential_ref=binding.credential_ref)
            source.protocol = binding.protocol
            source.base_url = binding.base_url
            config = _sparse_config(source)
            service, store, _ = _service(tmp_path / "service", config, adapter)
            adapter.state_store.sync_sources(service._bindings(config))
            handle = await adapter.invoke(source.id, MODEL, {}, True, "opencode")
            assert await anext(handle.stream) == FIRST
            result = await asyncio.wait_for(
                service.set_agent_chain("claude", MODEL, {"hops": [{"source_id": source.id, "model_id": "new"}]}),
                2,
            )
            assert result["chain"]["current"] == {"source_id": source.id, "model_id": "new"}
            assert store.config.agents["claude"].routes[MODEL].hops[0].model_id == "new"
            assert supervisor.reload_count == 1
            assert not service._mutation_lock.locked()
            finish.set()
            assert b"".join([part async for part in handle.stream]) == LAST
            async with service._mutation_lock:
                assert (await handle.outcome()).kind is RawOutcomeKind.SUCCESS
            assert adapter._active_transports == 0

    asyncio.run(run())


@pytest.mark.parametrize("finish_kind", ["unstarted_close", "started_close", "stream_cancel", "invoke_cancel"])
def test_close_and_cancellation_release_transport_without_waiting_for_outcome(tmp_path, finish_kind):
    # Leases still gate an engine binary upgrade restart.
    async def run():
        async with _transport(tmp_path) as (adapter, _supervisor, binding, received, _finish, _requests):
            streaming = finish_kind != "invoke_cancel"
            invoke = asyncio.create_task(adapter.invoke(binding.source_id, "unknown", {}, streaming, "opencode"))
            await asyncio.wait_for(received.wait(), 2)
            handle = await invoke if streaming else None
            pending_read = None
            if finish_kind in {"started_close", "stream_cancel"}:
                assert await anext(handle.stream) == FIRST
            if finish_kind == "stream_cancel":
                pending_read = asyncio.create_task(anext(handle.stream))
                await asyncio.sleep(0)
            assert not adapter._transports_idle.is_set()
            if handle is None:
                invoke.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await invoke
            elif pending_read is not None:
                pending_read.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending_read
                await handle.close_stream()
            else:
                await handle.close_stream()
                await handle.close_stream()
            await asyncio.wait_for(adapter._transports_idle.wait(), 2)
            assert adapter._active_transports == 0

    asyncio.run(run())
