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
        self.restart_started = threading.Event()
        self.allow_restart = threading.Event()
        self.allow_restart.set()
        self.restart_count = 0
        self.restore_count = 0
        self.fail_restart = False

    def client(self):
        return EngineClient(self.connection)

    def client_if_running(self):
        return self.client()

    def restart_if_running(self):
        self.restart_count += 1
        self.restart_started.set()
        assert self.allow_restart.wait(3)
        if self.fail_restart:
            raise EngineUnavailableError("models.engine.health_failed")

    def ensure_running(self):
        self.restore_count += 1
        return self.connection


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
        supervisor.allow_restart.set()
        await runner.cleanup()


async def _barrier_waiting(adapter, sync):
    async with asyncio.timeout(2):
        while not adapter._routing_lock.locked():
            assert not sync.done()
            await asyncio.sleep(0)
    assert not sync.done()


@pytest.mark.parametrize("stream", [False, True])
def test_sync_waits_for_buffered_and_streaming_transport_not_service_settlement(tmp_path, stream):
    async def run():
        async with _transport(tmp_path) as (adapter, supervisor, binding, received, finish, requests):
            invoke = asyncio.create_task(adapter.invoke(binding.source_id, "unknown", {}, stream, "opencode"))
            await asyncio.wait_for(received.wait(), 2)
            handle = await invoke if stream else None
            if handle is not None:
                assert await anext(handle.stream) == FIRST
            changed = replace(binding, route_model_ids=("unknown", "new"))
            sync = asyncio.create_task(adapter.sync_sources([changed]))
            await _barrier_waiting(adapter, sync)
            later = asyncio.create_task(adapter.invoke(binding.source_id, "new", {}, False, "opencode"))
            await asyncio.sleep(0)
            assert not later.done()
            assert supervisor.restart_count == 0
            assert len(requests) == 1
            # A service mutation lock can remain held during this drain. No
            # outcome()/settlement call is needed to release the transport.
            finish.set()
            if handle is not None:
                assert b"".join([part async for part in handle.stream]) == LAST
            else:
                handle = await asyncio.wait_for(invoke, 2)
            await asyncio.wait_for(sync, 2)
            assert supervisor.restart_count == 1
            assert (await handle.outcome()).kind is RawOutcomeKind.SUCCESS
            later_handle = await asyncio.wait_for(later, 2)
            await handle.close_stream()
            await later_handle.close_stream()
            assert adapter._active_transports == 0
            assert requests[1]["model"].endswith("/new")

    asyncio.run(run())


@pytest.mark.parametrize("finish_kind", ["unstarted_close", "started_close", "stream_cancel", "invoke_cancel"])
def test_close_and_cancellation_release_transport_without_waiting_for_outcome(tmp_path, finish_kind):
    async def run():
        async with _transport(tmp_path) as (adapter, supervisor, binding, received, _finish, _requests):
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
            sync = asyncio.create_task(adapter.sync_sources([replace(binding, route_model_ids=("new",))]))
            await _barrier_waiting(adapter, sync)
            assert supervisor.restart_count == 0
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
            await asyncio.wait_for(sync, 2)
            assert supervisor.restart_count == 1
            assert adapter._active_transports == 0
            assert adapter._transports_idle.is_set()

    asyncio.run(run())


def test_cancelled_drain_does_not_cancel_active_request_or_leak_barrier(tmp_path):
    async def run():
        async with _transport(tmp_path) as (adapter, supervisor, binding, _received, finish, _requests):
            handle = await adapter.invoke(binding.source_id, "unknown", {}, True, "opencode")
            assert await anext(handle.stream) == FIRST
            before = adapter.state_store.list_sources()
            sync = asyncio.create_task(adapter.sync_sources([replace(binding, route_model_ids=("new",))]))
            await _barrier_waiting(adapter, sync)
            sync.cancel()
            with pytest.raises(asyncio.CancelledError):
                await sync
            assert not adapter._routing_lock.locked()
            assert adapter.state_store.list_sources() == before
            assert adapter._active_transports == 1
            assert supervisor.restart_count == 0
            finish.set()
            assert b"".join([part async for part in handle.stream]) == LAST
            assert (await handle.outcome()).kind is RawOutcomeKind.SUCCESS
            await adapter.sync_sources([binding])
            assert adapter._active_transports == 0

    asyncio.run(run())


@pytest.mark.parametrize("fail_restart", [False, True])
def test_cancelled_restart_retains_barrier_until_commit_or_rollback(tmp_path, fail_restart):
    async def run():
        async with _transport(tmp_path) as (adapter, supervisor, binding, _received, finish, requests):
            original = adapter.state_store.list_sources()
            supervisor.allow_restart.clear()
            supervisor.fail_restart = fail_restart
            sync = asyncio.create_task(adapter.sync_sources([replace(binding, route_model_ids=("changed",))]))
            assert await asyncio.to_thread(supervisor.restart_started.wait, 2)
            sync.cancel()
            await asyncio.sleep(0)
            assert not sync.done()
            assert adapter._routing_lock.locked()
            later = asyncio.create_task(adapter.invoke(binding.source_id, "listed", {}, False, "opencode"))
            await asyncio.sleep(0)
            assert requests == []
            supervisor.allow_restart.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(sync, 2)
            finish.set()
            handle = await asyncio.wait_for(later, 2)
            assert (await handle.outcome()).kind is RawOutcomeKind.SUCCESS
            await handle.close_stream()
            if fail_restart:
                assert adapter.state_store.list_sources() == original
                assert supervisor.restore_count == 1
            else:
                assert adapter.state_store.list_sources()[0].route_model_ids == ("changed",)
            assert adapter._active_transports == 0
            assert not adapter._routing_lock.locked()

    asyncio.run(run())


def test_two_concurrent_saves_are_serial_and_restart_failure_restores_projection(tmp_path):
    async def run():
        async with _transport(tmp_path) as (adapter, supervisor, binding, _received, _finish, _requests):
            original = adapter.state_store.list_sources()
            supervisor.fail_restart = True
            with pytest.raises(EngineUnavailableError):
                await adapter.sync_sources([replace(binding, route_model_ids=("bad",))])
            assert adapter.state_store.list_sources() == original
            assert supervisor.restore_count == 1
            supervisor.fail_restart = False
            supervisor.restart_started.clear()
            supervisor.allow_restart.clear()
            first = asyncio.create_task(adapter.sync_sources([replace(binding, route_model_ids=("first",))]))
            assert await asyncio.to_thread(supervisor.restart_started.wait, 2)
            second = asyncio.create_task(adapter.sync_sources([replace(binding, route_model_ids=("second",))]))
            await asyncio.sleep(0)
            assert adapter.state_store.list_sources()[0].route_model_ids == ("first",)
            supervisor.allow_restart.set()
            await asyncio.wait_for(asyncio.gather(first, second), 2)
            assert adapter.state_store.list_sources()[0].route_model_ids == ("second",)
            assert supervisor.restart_count == 3
            assert not adapter._routing_lock.locked()

    asyncio.run(run())


def test_service_save_drain_releases_before_settlement_can_acquire_mutation_lock(tmp_path):
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
            save = asyncio.create_task(
                service.set_agent_chain("claude", MODEL, {"hops": [{"source_id": source.id, "model_id": "new"}]})
            )
            await _barrier_waiting(adapter, save)
            assert service._mutation_lock.locked()

            async def settle():
                async with service._mutation_lock:
                    return await handle.outcome()

            settlement = asyncio.create_task(settle())
            await asyncio.sleep(0)
            assert not settlement.done()
            assert supervisor.restart_count == 0
            finish.set()
            assert b"".join([part async for part in handle.stream]) == LAST
            result, outcome = await asyncio.wait_for(asyncio.gather(save, settlement), 2)
            assert result["chain"]["current"] == {"source_id": source.id, "model_id": "new"}
            assert outcome.kind is RawOutcomeKind.SUCCESS
            assert store.config.agents["claude"].routes[MODEL].hops[0].model_id == "new"
            assert supervisor.restart_count == 1
            assert adapter._active_transports == 0

    asyncio.run(run())
