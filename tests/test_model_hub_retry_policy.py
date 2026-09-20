"""Bounded recovery at the shared resolver and actual gateway HTTP boundary."""

from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace

import aiohttp
from aiohttp import web
import pytest

from config.v2_config import ModelHubBackendModelConfig, ModelHubSourceStateConfig
from core.handlers.model_hub.adapter import RawOutcomeKind
from core.handlers.model_hub.classification import ResolutionDecision
from core.handlers.model_hub.retry import (
    RECOVERY_EXHAUSTED_CODE, RECOVERY_EXHAUSTED_MESSAGE, RecoveryPolicy, retry_after_deadline,
)
from core.handlers.model_hub.service import ModelHubError
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from modules.agents.model_hub import ModelHubRuntimeRouter
from tests.test_model_hub_inference_wait import WIRE as INFERENCE_WIRE
from tests.test_model_hub_l3 import (
    NOW, _assert_valid, _canonicalize_fixed_test_routes, _outcome, _service, _source,
)
from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter
from vibe.model_hub_runtime.client import EngineClient, EngineConnection
from vibe.model_hub_runtime.state import EngineStateStore


# The inference-deadline fixtures deliberately exercise permissive responses.
# Recovery additionally needs recognized terminal envelopes.
WIRE = {
    **INFERENCE_WIRE,
    "openai_responses": (*INFERENCE_WIRE["openai_responses"][:3],
        b'{"object":"response","status":"completed","output":[{"content":[{"type":"output_text","text":"ok"}]}]}'),
    "openai_chat": (*INFERENCE_WIRE["openai_chat"][:3],
        b'{"object":"chat.completion","choices":[{"message":{"role":"assistant","content":"ok"}}]}'),
}


class Clock:
    def __init__(self):
        self.elapsed = 0.0
        self.offset = 0.0
        self.delays = []

    def now(self):
        return NOW + timedelta(seconds=self.elapsed + self.offset)

    def advance(self, seconds):
        self.elapsed += seconds

    async def sleep(self, seconds):
        self.delays.append(seconds)
        self.advance(seconds)
        await asyncio.sleep(0)

    def policy(self):
        return RecoveryPolicy(
            now=self.now, monotonic=lambda: self.elapsed,
            jitter=lambda low, high: low, sleep=self.sleep,
        )


def clock_service(tmp_path, *, sources=None, outcomes=None):
    clock = Clock()
    service = _service(
        tmp_path, sources=sources or [_source("src_recovery01", "Recovering")],
        outcomes=outcomes,
    )
    service.now = clock.now
    service.recovery = clock.policy()
    # Keep the exact common fixture route as a real catalog model so health
    # persistence validates the same shape used by the inference tests.
    for backend in ("claude", "codex"):
        service.store.config.agents[backend].models.append(
            ModelHubBackendModelConfig(id="shared-model", origin="manual"),
        )
    return service, clock


async def fail(service, source, reason="network", *, generation=None, outcome=None):
    generation = generation if generation is not None else service._reserve_settlement_generation(source.id)
    return await service._settle_fallback_source(
        source, ResolutionDecision("fallback", reason=reason),
        backend="codex", model_id="shared-model", settlement_generation=generation, outcome=outcome,
    )


@pytest.mark.parametrize("reason,delays", [
    ("network", [1, 2, 4, 8, 16, 30, 30]),
    ("server_error", [30, 60, 120, 120]),
    ("rate_limited", [60, 120, 240, 300, 300]),
    ("quota_exhausted", [300, 300]),
])
def test_shared_streak_counts_attempts_and_caps_positive_jitter(tmp_path, reason, delays):
    async def run():
        service, clock = clock_service(tmp_path)
        source = service.store.load().sources[0]
        before = copy.deepcopy(service.store.load().to_payload())
        for expected in delays:
            await fail(service, source, reason)
            annotation = service.recovery.annotations(service.store.load())[source.id]
            assert (datetime.fromisoformat(annotation.retry_at) - clock.now()).total_seconds() == expected
            clock.advance(expected)
        if reason == "network":
            assert service.store.load().to_payload() == before
        service.recovery.jitter = lambda low, high: high
        service.recovery._sources.clear()
        if reason != "network":
            source = service.store.load().sources[0]
            source.state = ModelHubSourceStateConfig(status="standby")
        await fail(service, source, reason)
        annotation = service.recovery.annotations(service.store.load())[source.id]
        assert (datetime.fromisoformat(annotation.retry_at) - clock.now()).total_seconds() == min(delays[0] * 1.2, delays[-1])
    asyncio.run(run())


@pytest.mark.parametrize("value", [None, "", "-1", "+1", "1.5", "NaN", "x" * 129, "４", "9" * 128, "Wed, 01 Jan 2020 00:00:00 GMT"])
def test_invalid_advice_cannot_replace_local_policy(value):
    assert retry_after_deadline(value, NOW) is None


def test_retry_after_uses_response_receipt_and_keeps_long_upstream_advice(tmp_path):
    async def run():
        service, clock = clock_service(tmp_path)
        source = service.store.load().sources[0]
        for advice in ("300", format_datetime(NOW + timedelta(seconds=300), usegmt=True)):
            service.recovery._sources.clear()
            clock.elapsed = 20
            outcome = SimpleNamespace(retry_after=advice, response_received_at=NOW, stream_started=False)
            await fail(service, source, "server_error", outcome=outcome)
            assert service.recovery.annotations(service.store.load())[source.id].retry_at == (NOW + timedelta(seconds=300)).isoformat()
            with pytest.raises(ModelHubError):
                await service.resolve_with_recovery(
                    backend="codex", model_id="shared-model", request={}, supply_channel="hub",
                )
            assert clock.delays == []
            assert service.adapter.invocations == []
    asyncio.run(run())


def test_offsets_and_wall_clock_shifts_do_not_change_monotonic_admission(tmp_path):
    async def run():
        service, clock = clock_service(tmp_path)
        source = service.store.load().sources[0]
        await fail(service, source)
        clock.offset = -3600
        hop = service.agent_chain("codex", "shared-model")["chain"][0]
        assert (datetime.fromisoformat(hop["retry_at"]) - clock.now()).total_seconds() == 1
        clock.advance(1)
        assert service.agent_chain("codex", "shared-model")["chain"][0]["recovery"] == "eligible"
        assert service.store.load().sources[0].state.status == "standby"
        assert not any(event["kind"] == "recover" for event in service.events.list())
    asyncio.run(run())


@pytest.mark.parametrize("blocker", ["none", "cooldown", "needs_action", "error", "retired", "missing"])
def test_live_network_overlay_preserves_stronger_blockers(tmp_path, blocker):
    async def run():
        service, clock = clock_service(tmp_path)
        source = service.store.load().sources[0]
        await fail(service, source)
        if blocker in {"cooldown", "needs_action", "error"}:
            source.state = ModelHubSourceStateConfig(
                status=blocker,
                retry_at=(clock.now() + timedelta(seconds=60)).isoformat() if blocker == "cooldown" else None,
                detail_key={
                    "cooldown": "models.source.cooldown.rate_limited",
                    "needs_action": "models.source.needs_action.credential_revoked",
                    "error": "models.source.error.unclassified",
                }[blocker],
            )
        if blocker == "retired":
            source.models[0].retired = True
        if blocker == "missing":
            service.store.config.sources = []
        chain = service.agent_chain("codex", "shared-model")
        _assert_valid("agent-chain.schema.json", chain)
        hop = chain["chain"][0]
        assert not hop["runnable"]
        assert hop["health"] == {
            "none": "backoff", "cooldown": "cooldown", "retired": "healthy",
            "missing": "error", "needs_action": "needs_action", "error": "error",
        }[blocker]
        assert chain["supply_state"] == ("waiting" if blocker in {"none", "cooldown"} else "interrupted")
        agents = {agent["backend"]: agent for agent in service.list_agents()}
        assert agents["codex"]["supply_status"] == chain["supply_state"]
    asyncio.run(run())


def test_stale_duplicate_settlement_and_replacement_cannot_advance_or_unlock(tmp_path):
    async def run():
        service, clock = clock_service(tmp_path)
        source = service.store.load().sources[0]
        first = service._reserve_settlement_generation(source.id)
        await fail(service, source, generation=first)
        await fail(service, source, generation=first)
        assert service.recovery._sources[source.id].failures == 1
        clock.advance(1)
        second = service._reserve_settlement_generation(source.id)
        assert service.recovery.claim(source, second)
        service.recovery.release(source.id, first)
        assert service.recovery._sources[source.id].owner == second
        await fail(service, source, generation=first)
        assert service.recovery._sources[source.id].owner == second
        old = copy.deepcopy(source)
        source.credential_ref = "cred_replaced"
        assert service.recovery.annotations(service.store.load()) == {}
        await fail(service, old, generation=second)
        assert service.recovery.annotations(service.store.load()) == {}
        assert source.state.status == "standby"
    asyncio.run(run())


def test_fallback_success_does_not_clear_primary_and_eligibility_is_not_recovery(tmp_path):
    async def run():
        first, second = _source("src_primary01", "Primary"), _source("src_backup001", "Backup")
        service, clock = clock_service(tmp_path, sources=[first, second], outcomes=[
            _outcome(RawOutcomeKind.NETWORK_ERROR, source_id=first.id),
            _outcome(RawOutcomeKind.SUCCESS, source_id=second.id, stream_started=True),
        ])
        result = await service.resolve_with_recovery(backend="codex", model_id="shared-model", request={})
        assert result.source_id == second.id
        assert clock.delays == []
        assert first.id in service.recovery._sources
        assert not any(event["kind"] == "recover" for event in service.events.list())
    asyncio.run(run())


def test_one_window_covers_all_fallback_passes_without_replaying_after_output(tmp_path):
    """MH-RETRY-WINDOW-001: admission shares one clock across failed attempts."""
    async def run():
        service, clock = clock_service(tmp_path, outcomes=[
            _outcome(RawOutcomeKind.NETWORK_ERROR, source_id="src_recovery01")
            for _ in range(20)
        ])
        snapshots = []
        with pytest.raises(ModelHubError):
            await service.resolve_with_recovery(
                backend="codex", model_id="shared-model", request={}, recovery_observer=snapshots.append,
            )
        assert clock.delays == [1, 2, 4, 8, 16, 30, 30]
        assert len(service.adapter.invocations) == 8
        assert clock.elapsed == 91
        assert snapshots[-1] is None
        assert {snapshot["window_end"] for snapshot in snapshots if snapshot} == {(NOW + timedelta(seconds=120)).isoformat()}
        assert {snapshot["started_at"] for snapshot in snapshots if snapshot} == {NOW.isoformat()}
    asyncio.run(run())


@pytest.mark.parametrize("backend", ["codex", "claude", "opencode"])
def test_startup_passes_temporary_hub_supply_without_spending_another_window(tmp_path, backend):
    async def run():
        service, clock = clock_service(tmp_path)
        models = _canonicalize_fixed_test_routes(service)
        source = service.store.load().sources[0]
        await fail(service, source)
        gateway = ModelHubTurnGateway(service)
        router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway, overlay_path=tmp_path / "overlay.json")
        try:
            model = models.get(backend, "shared-model")
            launch = await router.resolve(backend, model, process_scope="/repo", turn_id="turn_startup")
            assert launch.channel == "hub" and launch.source_id == source.id
            if backend == "opencode":
                overlay = await router.prepare_opencode_overlay()
                assert "shared-model" in overlay.available_identifiers
            assert clock.delays == []
            assert service.adapter.invocations == []
            assert gateway.correlation.recovery_snapshot("turn_startup") == []
        finally:
            await gateway.close()
    asyncio.run(run())


@pytest.mark.parametrize("protocol", WIRE)
@pytest.mark.parametrize("streaming,verified,empty", [
    (False, True, False), (True, True, False), (False, False, False),
    (False, True, True), (True, True, True),
])
def test_real_gateway_retries_preoutput_then_preserves_one_response(tmp_path, protocol, streaming, verified, empty):
    """MH-RETRY-RECOVERY-001 / D8: real loopback evidence, never timer-based recovery."""

    async def run():
        calls = []
        metadata, output, terminal, buffered = WIRE[protocol]
        if not verified:
            buffered = b'{"unknown":"permissive compatibility response"}'
        if empty:
            output = b""
            buffered = {
                "openai_responses": b'{"object":"response","status":"completed","output":[]}',
                "openai_chat": b'{"object":"chat.completion","choices":[]}',
                "anthropic": b'{"type":"message","content":[]}',
            }[protocol]

        async def respond(request):
            calls.append(await request.json())
            if len(calls) <= 2:
                return web.json_response({"error": {"type": "server_error"}}, status=503)
            # An admitted recovery attempt may think beyond the 120s window.
            clock.advance(121)
            return web.Response(
                body=metadata + output + terminal if streaming else buffered,
                content_type="text/event-stream" if streaming else "application/json",
            )

        app = web.Application()
        app.router.add_post("/{path:.*}", respond)
        runner = web.AppRunner(app, handler_cancellation=True)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        backend, endpoint = {
            "openai_responses": ("codex", "responses"),
            "openai_chat": ("opencode", "chat/completions"),
            "anthropic": ("claude", "messages"),
        }[protocol]
        source = _source("src_loopback01", "Loopback", protocol=protocol, vendor="anthropic" if backend == "claude" else "openai")
        state = EngineStateStore(tmp_path / "engine")
        source.credential_ref = state.store_api_key("fixture-key", vendor=source.vendor, protocol=protocol)
        service, clock = clock_service(tmp_path, sources=[source])
        models = _canonicalize_fixed_test_routes(service)
        state.sync_sources(service._bindings(service.store.load()))
        client = EngineClient(
            EngineConnection(f"http://127.0.0.1:{runner.addresses[0][1]}", "fixture-management", "fixture-gateway"),
            timeout=0.1,
        )
        adapter = CLIProxyEngineAdapter(supervisor=SimpleNamespace(client=lambda: client), state_store=state)
        adapter.ensure_installed = service.adapter.ensure_installed
        service.adapter = adapter
        service._engine_synced = True
        gateway = ModelHubTurnGateway(service)
        base_url, token = await gateway.endpoint(
            backend, process_scope="/repo", turn_id="turn_recovered",
            requested_model_id=models.get(backend, "shared-model"),
            resolved_model_id="shared-model", source_id=source.id,
        )
        try:
            async with aiohttp.ClientSession(trust_env=False) as downstream:
                async with downstream.post(
                    f"{base_url}/v1/{endpoint}",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"model": "shared-model", "stream": streaming},
                ) as response:
                    assert response.status == 200
                    assert await response.read() == (metadata + output + terminal if streaming else buffered)
            assert len(calls) == 3
            assert calls[0] == calls[1] == calls[2]
            assert clock.delays == [30, 60]
            assert adapter._active_transports == 0
            assert gateway.correlation.recovery_snapshot("turn_recovered") == []
            assert (source.id not in service.recovery._sources) is verified
            assert sum(event["kind"] == "recover" for event in service.events.list()) == int(verified)
        finally:
            await gateway.close()
            await runner.cleanup()
    asyncio.run(run())


@pytest.mark.parametrize("protocol", WIRE)
@pytest.mark.parametrize("ending", ["cancel_owner", "slow_owner"])
def test_real_gateway_has_one_half_open_owner_and_interruptible_waiters(tmp_path, protocol, ending):
    """MH-RETRY-CONCURRENCY-001: real HTTP owners and waiters release only their slot."""
    async def run():
        connected, release, owner_closed, waiting = (asyncio.Event() for _ in range(4))
        calls = []
        buffered = WIRE[protocol][3]

        async def respond(request):
            calls.append(await request.json())
            if len(calls) == 1:
                connected.set()
                try:
                    await release.wait()
                finally:
                    owner_closed.set()
            return web.Response(body=buffered, content_type="application/json")

        app = web.Application()
        app.router.add_post("/{path:.*}", respond)
        runner = web.AppRunner(app, handler_cancellation=True)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        backend, endpoint = {
            "openai_responses": ("codex", "responses"),
            "openai_chat": ("opencode", "chat/completions"),
            "anthropic": ("claude", "messages"),
        }[protocol]
        source = _source("src_concurrent1", "Concurrent", protocol=protocol)
        state = EngineStateStore(tmp_path / "engine")
        source.credential_ref = state.store_api_key("fixture-key", vendor=source.vendor, protocol=protocol)
        service, clock = clock_service(tmp_path, sources=[source])
        models = _canonicalize_fixed_test_routes(service)
        await fail(service, source)
        clock.advance(1)
        state.sync_sources(service._bindings(service.store.load()))
        client = EngineClient(
            EngineConnection(f"http://127.0.0.1:{runner.addresses[0][1]}", "fixture-management", "fixture-gateway"),
            timeout=0.1,
        )
        adapter = CLIProxyEngineAdapter(supervisor=SimpleNamespace(client=lambda: client), state_store=state)
        adapter.ensure_installed = service.adapter.ensure_installed
        service.adapter = adapter
        service._engine_synced = True

        async def wait_until_changed(delay):
            waiting.set()
            await asyncio.Future()

        service.recovery.sleep = wait_until_changed
        gateway = ModelHubTurnGateway(service)
        model = models.get(backend, "shared-model")
        base_url, first_token = await gateway.endpoint(
            backend, process_scope="owner", turn_id="turn_owner",
            requested_model_id=model, resolved_model_id="shared-model", source_id=source.id,
        )
        _, second_token = await gateway.endpoint(
            backend, process_scope="waiter", turn_id="turn_waiter",
            requested_model_id=model, resolved_model_id="shared-model", source_id=source.id,
        )
        tasks = []
        try:
            async with aiohttp.ClientSession(trust_env=False) as downstream:
                def post(token):
                    return asyncio.create_task(downstream.post(
                        f"{base_url}/v1/{endpoint}",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"model": "shared-model", "stream": False},
                    ))

                owner = post(first_token)
                tasks.append(owner)
                await asyncio.wait_for(connected.wait(), 2)
                generation = service.recovery._sources[source.id].owner
                assert generation is not None
                waiter = post(second_token)
                tasks.append(waiter)
                await asyncio.wait_for(waiting.wait(), 2)
                assert len(calls) == 1 and not waiter.done()
                chain = service.agent_chain(backend, model)
                assert chain["chain"][0]["recovery"] == "in_flight"
                assert chain["chain"][0]["runnable"] is False
                assert chain["supply_state"] == "waiting"
                _assert_valid("agent-chain.schema.json", chain)
                snapshot = gateway.correlation.recovery_snapshot("turn_waiter")
                if backend == "opencode":
                    # Shared-server requests have no exact Turn owner yet.
                    assert snapshot == []
                else:
                    assert len(snapshot) == 1 and snapshot[0]["phase"] == "waiting"
                if ending == "cancel_owner":
                    owner.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await owner
                    await asyncio.wait_for(owner_closed.wait(), 2)
                    response = await asyncio.wait_for(waiter, 2)
                    assert response.status == 200 and await response.read() == buffered
                    assert len(calls) == 2
                else:
                    clock.advance(121)
                    service.recovery.notify()
                    response = await asyncio.wait_for(waiter, 2)
                    assert response.status == (400 if backend == "codex" else 424)
                    assert "Retry-After" not in response.headers
                    assert await response.json() == {
                        "type": "error",
                        "error": {
                            "type": RECOVERY_EXHAUSTED_CODE,
                            "code": RECOVERY_EXHAUSTED_CODE,
                            "message": RECOVERY_EXHAUSTED_MESSAGE,
                        },
                    }
                    assert len(calls) == 1 and not owner.done()
                    assert service.recovery._sources[source.id].owner == generation
                    release.set()
                    response = await asyncio.wait_for(owner, 2)
                    assert response.status == 200 and await response.read() == buffered
                await gateway._drain_turn_requests("turn_owner")
                await gateway._drain_turn_requests("turn_waiter")
                assert adapter._active_transports == 0
                assert source.id not in service.recovery._sources
                assert gateway.correlation.recovery_snapshot("turn_waiter") == []
                assert sum(event["kind"] == "recover" for event in service.events.list()) == 1
        finally:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await gateway.close()
            await runner.cleanup()
    asyncio.run(run())


def test_permissive_success_releases_admission_without_verified_recovery(tmp_path):
    async def run():
        service, clock = clock_service(tmp_path, outcomes=[
            _outcome(RawOutcomeKind.SUCCESS, source_id="src_recovery01", stream_started=True),
        ])
        source = service.store.load().sources[0]
        source.verification_pending = "vp_fixture"
        await fail(service, source)
        clock.advance(1)
        result = await service.resolve_with_recovery(backend="codex", model_id="shared-model", request={})
        assert result.outcome.kind is RawOutcomeKind.SUCCESS
        entry = service.recovery._sources[source.id]
        assert entry.owner is None and entry.failures == 1
        assert service.agent_chain("codex", "shared-model")["chain"][0]["recovery"] == "eligible"
        assert not any(event["kind"] == "recover" for event in service.events.list())
    asyncio.run(run())


def test_draft_preview_does_not_clear_live_health_and_mixed_action_blocks_waiting(tmp_path):
    async def run():
        first, second = _source("src_primary01", "Primary"), _source("src_backup001", "Backup")
        service, clock = clock_service(tmp_path, sources=[first, second])
        await fail(service, first)
        draft = copy.deepcopy(service.store.load())
        draft.sources[0].credential_ref = "cred_draft"
        assert first.id not in service.recovery_annotations(draft)
        assert first.id in service.recovery_annotations(service.store.load())
        second.state = ModelHubSourceStateConfig(
            status="needs_action", detail_key="models.source.needs_action.credential_revoked",
        )
        with pytest.raises(ModelHubError) as failed:
            await service.resolve_with_recovery(backend="codex", model_id="shared-model", request={})
        assert failed.value.supply_state == "interrupted"
        assert clock.delays == [] and service.adapter.invocations == []
    asyncio.run(run())


@pytest.mark.parametrize("read", ["agent_chain", "agent_chains", "get_agent_sources", "list_agents"])
def test_public_live_reads_capture_config_and_health_once(tmp_path, read):
    async def run():
        service, _clock = clock_service(tmp_path)
        await fail(service, service.store.load().sources[0])
        counts = {"config": 0, "health": 0}
        load, annotations = service.store.load, service.recovery.annotations

        def capture_config():
            counts["config"] += 1
            return load()

        def capture_health(config):
            counts["health"] += 1
            return annotations(config)

        service.store.load = capture_config
        service.recovery.annotations = capture_health
        args = {
            "agent_chain": ("codex", "shared-model"),
            "agent_chains": ("codex",),
            "get_agent_sources": ("codex",),
            "list_agents": (),
        }[read]
        result = getattr(service, read)(*args)
        assert result
        assert counts == {"config": 1, "health": 1}
    asyncio.run(run())


def test_half_open_projection_cannot_hide_action_or_native_blockers(tmp_path):
    async def run():
        service, clock = clock_service(tmp_path)
        source = service.store.load().sources[0]
        await fail(service, source)
        clock.advance(1)
        generation = service._reserve_settlement_generation(source.id)
        assert service.recovery.claim(source, generation)
        chain = service.agent_chain("codex", "shared-model")
        assert chain["chain"][0]["recovery"] == "in_flight"
        _assert_valid("agent-chain.schema.json", chain)
        for mutation in [
            {"runnable": True},
            {"health": "needs_action", "reason": "models.source.needs_action.credential_revoked"},
            {"reason": "source_missing"},
            {"channel": "native_cli", "reason": "native_cli_unavailable"},
            {"recovery": "eligible"},
        ]:
            invalid = copy.deepcopy(chain)
            invalid["chain"][0].update(mutation)
            with pytest.raises(AssertionError):
                _assert_valid("agent-chain.schema.json", invalid)
        source.state = ModelHubSourceStateConfig(
            status="needs_action", detail_key="models.source.needs_action.credential_revoked",
        )
        blocked = service.agent_chain("codex", "shared-model")
        assert "recovery" not in blocked["chain"][0]
        assert blocked["supply_state"] == "interrupted"
        _assert_valid("agent-chain.schema.json", blocked)
        for phase in ("eligible", "in_flight"):
            blocked["chain"][0]["recovery"] = phase
            with pytest.raises(AssertionError):
                _assert_valid("agent-chain.schema.json", blocked)
    asyncio.run(run())


@pytest.mark.parametrize("kind,suffix", [
    (RawOutcomeKind.NETWORK_ERROR, "network"), (RawOutcomeKind.TIMEOUT, "timeout"),
])
def test_selected_source_probe_does_not_claim_or_schedule_recovery(tmp_path, kind, suffix):
    async def run():
        service, clock = clock_service(tmp_path, outcomes=[_outcome(kind, source_id="src_recovery01")])
        before = copy.deepcopy(service.store.load().to_payload())
        result = await service.probe_source("src_recovery01", {"model": "shared-model"})
        assert result["reachable"] is False
        assert result["error"] == f"models.source.cooldown.{suffix}"
        assert len(service.adapter.invocations) == 1
        assert service.recovery._sources == {} and clock.delays == []
        assert service.store.load().to_payload() == before
        assert service.events.list() == []
    asyncio.run(run())
