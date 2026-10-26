"""Buffering must preserve caller identity, recovery clocks, and cancellation.

These are distinct from the exact-error matrix: they exercise real in-flight
HTTP ownership and the Vibey pin/fallback boundary, which fixed response bodies
cannot protect.
"""

import asyncio
import json

import aiohttp
from aiohttp import web
import pytest

from core.handlers.model_hub import turn_gateway
from core.handlers.model_hub.provenance import SERVED_HOP_HEADER
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from core.run_settlement import SETTLED_BY_TERMINAL_RESULT
from tests.test_model_hub_l3 import _source, _usage_of
from tests.test_model_hub_retry_policy import Clock, WIRE
from tests.test_model_hub_vibey_consumer import _vibey_service
from tests.test_model_hub_retry_lifecycle import configured_service, gateway_client, loopback_engine
from tests.test_model_hub_streamed_overload import OVERLOAD, output, ending


@pytest.mark.parametrize("pinned", [False, True])
def test_vibey_overload_uses_backup_without_leaking_origin_or_pinned_content(tmp_path, pinned):
    """MH-RETRY-OVERLOAD-001: real Vibey caller, fallback headers and pin handback."""
    async def run():
        primary = _source("src_primary01", "Primary")
        backup = _source("src_backup001", "Backup")
        service = _vibey_service(tmp_path, [primary, backup])
        clock = Clock()
        service.now, service.recovery = clock.now, clock.policy()
        calls = []
        first = output("discarded") + ending(error="server_error")
        final = output("backup") + ending()

        async def respond(request):
            calls.append(await request.json())
            return web.Response(body=first if len(calls) == 1 else final, content_type="text/event-stream")

        async with loopback_engine(tmp_path, service, respond) as (adapter, _):
            gateway = ModelHubTurnGateway(service)
            base, token = await gateway.endpoint(
                "vibey", process_scope="buffered", turn_id="turn_vibey",
                requested_model_id="menu-alias", resolved_model_id="shared-model", source_id=primary.id,
            )
            try:
                headers = {"Authorization": f"Bearer {token}", "x-avibe-turn-id": "turn_vibey"}
                if pinned:
                    headers.update({"x-avibe-pin-source": primary.id, "x-avibe-pin-model": "shared-model"})
                async with aiohttp.ClientSession(trust_env=False) as client:
                    async with client.post(
                        f"{base}/v1/responses", headers=headers,
                        json={"model": "shared-model", "stream": True, "input": "fixture prompt"},
                    ) as response:
                        if pinned:
                            assert response.status == 409
                            assert (await response.json())["error"]["code"] == "pin_unavailable"
                            assert SERVED_HOP_HEADER not in response.headers
                        else:
                            assert response.status == 200 and await response.read() == final
                            assert json.loads(response.headers[SERVED_HOP_HEADER])["source"] == backup.id
                await gateway._drain_turn_requests("turn_vibey")
                assert len(calls) == (1 if pinned else 2)
                assert clock.delays == []  # A ready backup is not the cooling primary.
                assert adapter._active_transports == 0
                assert _usage_of(service, primary.id)["requests"] == 1
                if pinned:
                    assert gateway.correlation.terminal_projection("turn_vibey", backend="vibey") is None
                else:
                    assert _usage_of(service, backup.id)["requests"] == 1
                    gateway.correlation.settle("turn_vibey", settled_by=SETTLED_BY_TERMINAL_RESULT)
                    assert service.provenance.get("turn_vibey")["served"]["source_id"] == backup.id
            finally:
                await gateway.close()
    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_initial_response_waits_for_terminal_and_owns_spool_until_cleanup(tmp_path, monkeypatch, cancel):
    """MH-RETRY-OVERLOAD-002: no early downstream bytes, bounded RAM, disconnect cleanup."""
    async def run():
        service, _clock, models = configured_service(tmp_path)
        prefix_sent, release, upstream_closed = (asyncio.Event() for _ in range(3))
        body = output("before-terminal-" + "x" * 300_000)
        files = []
        create = turn_gateway.tempfile.SpooledTemporaryFile

        def track_file(*args, **kwargs):
            payload = create(*args, **kwargs)
            files.append(payload)
            return payload

        monkeypatch.setattr(turn_gateway.tempfile, "SpooledTemporaryFile", track_file)

        async def respond(request):
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            try:
                await response.write(body)
                prefix_sent.set()
                await release.wait()
                await response.write(ending())
                await response.write_eof()
                return response
            finally:
                upstream_closed.set()

        async with loopback_engine(tmp_path, service, respond) as (adapter, _):
            async with gateway_client(service, "codex", models["codex"]) as (gateway, client, url, headers):
                task = asyncio.create_task(client.post(url, headers=headers, json={"model": "shared-model", "stream": True}))
                try:
                    await asyncio.wait_for(prefix_sent.wait(), 2)
                    assert not (await asyncio.wait({task}, timeout=0.05))[0], "headers committed before terminal"
                    if cancel:
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await task
                        await asyncio.wait_for(upstream_closed.wait(), 2)
                    else:
                        release.set()
                        response = await asyncio.wait_for(task, 2)
                        assert await response.read() == body + ending()
                    await gateway._drain_turn_requests("turn_lifecycle")
                    assert adapter._active_transports == 0
                    assert files and all(payload.closed for payload in files)
                    assert any(payload._rolled for payload in files)
                    assert _usage_of(service, service.store.load().sources[0].id)["requests"] == 1
                finally:
                    release.set()
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize("late_seconds,expected_calls", [(0, 3), (121, 2)])
def test_replay_keeps_preoutput_recovery_window(tmp_path, late_seconds, expected_calls):
    """MH-RETRY-OVERLOAD-003: an earlier refusal's window must not reset at replay."""
    async def run():
        service, clock, models = configured_service(tmp_path)
        calls = []
        first = output("discarded") + ending(error="server_error")
        final = output("final") + ending()

        async def respond(request):
            calls.append(await request.json())
            if len(calls) == 1:
                return web.json_response({"error": {"type": "server_error"}}, status=503)
            if len(calls) == 2:
                clock.advance(late_seconds)
            else:
                assert not any(event["kind"] == "recover" for event in service.events.list())
            return web.Response(body=first if len(calls) == 2 else final, content_type="text/event-stream")

        async with loopback_engine(tmp_path, service, respond):
            async with gateway_client(service, "codex", models["codex"]) as (gateway, client, url, headers):
                async with client.post(url, headers=headers, json={"model": "shared-model", "stream": True}) as response:
                    if late_seconds:
                        assert response.status == 400
                        assert (await response.json())["error"]["code"] == "model_hub_recovery_exhausted"
                    else:
                        assert response.status == 200 and await response.read() == final
                await gateway._drain_turn_requests("turn_lifecycle")
                assert len(calls) == expected_calls
                assert clock.delays == ([30] if late_seconds else [30, 60])
                assert sum(event["kind"] == "recover" for event in service.events.list()) == (0 if late_seconds else 1)
                assert gateway.correlation.recovery_snapshot("turn_lifecycle") == []
    asyncio.run(run())


@pytest.mark.parametrize("backend,protocol", [("claude", "anthropic"), ("opencode", "openai_chat")])
def test_other_protocols_still_stream_before_terminal_and_never_replay(tmp_path, backend, protocol):
    """MH-RETRY-OVERLOAD-001: the exception must not broaden to other wire protocols."""
    async def run():
        source = _source("src_other001", "Other", protocol=protocol, vendor="anthropic" if backend == "claude" else "openai")
        service, clock, models = configured_service(tmp_path, sources=[source])
        prefix = WIRE[protocol][0] + WIRE[protocol][1]
        error = (b'data: ' if protocol == "openai_chat" else b'event: error\ndata: ') + json.dumps({
            "type": "error", "error": {"type": "server_error", "message": OVERLOAD},
        }).encode() + b"\n\n"
        release = asyncio.Event()
        calls = 0

        async def respond(request):
            nonlocal calls
            calls += 1
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(prefix)
            await release.wait()
            await response.write(error)
            return response

        async with loopback_engine(tmp_path, service, respond):
            async with gateway_client(service, backend, models[backend]) as (_, client, url, headers):
                try:
                    response = await asyncio.wait_for(client.post(
                        url, headers=headers, json={"model": "shared-model", "stream": True},
                    ), 2)
                    assert await asyncio.wait_for(response.content.readexactly(len(prefix)), 2) == prefix
                    release.set()
                    assert await response.read() == error
                    assert calls == 1 and clock.delays == []
                finally:
                    release.set()
    asyncio.run(run())


@pytest.mark.parametrize("stage", ["write", "read", "limit"])
def test_spool_io_failure_is_local_metered_and_never_replayed(tmp_path, monkeypatch, stage):
    """MH-RETRY-OVERLOAD-002: added disk I/O cannot lose usage or blame the provider."""
    async def run():
        service, clock, models = configured_service(tmp_path)
        original = turn_gateway.tempfile.SpooledTemporaryFile
        files = []
        calls = 0

        def faulty_spool(*args, **kwargs):
            payload = original(*args, **kwargs)
            files.append(payload)

            def fail_io(*_args):
                raise OSError(28 if stage == "write" else 5, "fixture local disk error")

            if stage != "limit":
                setattr(payload, stage, fail_io)
            return payload

        monkeypatch.setattr(turn_gateway.tempfile, "SpooledTemporaryFile", faulty_spool)
        if stage == "limit":
            monkeypatch.setattr(turn_gateway, "_MAX_REPLAY_BUFFER_BYTES", len(output("answer") + ending()) - 1)

        async def respond(request):
            nonlocal calls
            calls += 1
            return web.Response(body=output("answer") + ending(), content_type="text/event-stream")

        async with loopback_engine(tmp_path, service, respond) as (adapter, _):
            async with gateway_client(service, "codex", models["codex"]) as (gateway, client, url, headers):
                async with client.post(url, headers=headers, json={"model": "shared-model", "stream": True}) as response:
                    body = await response.read()
                    assert response.status == (200 if stage == "read" else 502)
                    assert b"engine_down" in body
                await gateway._drain_turn_requests("turn_lifecycle")
                assert adapter._active_transports == 0 and clock.delays == [] and calls == 1
                assert files and all(payload.closed for payload in files)
                source = service.store.load().sources[0]
                assert _usage_of(service, source.id)["requests"] == 1
                assert source.state.status == "standby"
                gateway.correlation.settle("turn_lifecycle", settled_by=SETTLED_BY_TERMINAL_RESULT)
                assert service.provenance.get("turn_lifecycle")["terminal_error"]["reason"] == "engine_down"
    asyncio.run(run())


def test_cancellation_during_replay_wait_keeps_only_the_metered_failed_attempt(tmp_path):
    """MH-RETRY-OVERLOAD-002: dropping the first body must reset every attempt-owned debt."""
    async def run():
        service, _clock, models = configured_service(tmp_path)
        waiting = asyncio.Event()
        calls = 0

        async def wait_for_cancel(_seconds):
            waiting.set()
            await asyncio.Future()

        service.recovery.sleep = wait_for_cancel

        async def respond(request):
            nonlocal calls
            calls += 1
            return web.Response(body=output("discarded") + ending(error="server_error"), content_type="text/event-stream")

        async with loopback_engine(tmp_path, service, respond) as (adapter, _):
            async with gateway_client(service, "codex", models["codex"]) as (gateway, client, url, headers):
                task = asyncio.create_task(client.post(url, headers=headers, json={"model": "shared-model", "stream": True}))
                try:
                    await asyncio.wait_for(waiting.wait(), 2)
                    progress = gateway.correlation.recovery_snapshot("turn_lifecycle")
                    assert progress and progress[0]["phase"] == "waiting"
                    assert gateway.correlation.terminal_projection("turn_lifecycle", backend="codex") is None
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await gateway._drain_turn_requests("turn_lifecycle")
                assert adapter._active_transports == 0 and calls == 1
                source = service.store.load().sources[0]
                assert _usage_of(service, source.id)["requests"] == 1
                assert service.recovery._sources[source.id].owner is None
                assert gateway.correlation.recovery_snapshot("turn_lifecycle") == []
    asyncio.run(run())
