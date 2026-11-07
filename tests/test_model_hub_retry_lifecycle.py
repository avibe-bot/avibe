"""Recovery ownership and persistence boundaries from the first exact-head review."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import aiohttp
from aiohttp import web
import pytest

from config.v2_config import ModelHubConfig, ModelHubRouteConfig, ModelHubRouteHopConfig, ModelHubSourceStateConfig
from core.handlers.model_hub.adapter import RawOutcomeKind
from core.handlers.model_hub.retry import RECOVERY_EXHAUSTED_CODE, RECOVERY_EXHAUSTED_MESSAGE
from core.handlers.model_hub.service import ModelHubError, ModelHubService
from core.handlers.model_hub.stream_wire import ProtocolSSEState
from core.handlers.model_hub import turn_gateway
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from core.run_settlement import SETTLED_BY_TERMINAL_RESULT
from modules.agents.model_hub import ModelHubRuntimeRouter, bind_launch
from tests.test_model_hub_l3 import _assert_valid, _canonicalize_fixed_test_routes, _outcome, _source
from tests.test_model_hub_retry_policy import WIRE, clock_service, fail
from vibe.model_hub_runtime import adapter as engine_adapter_module
from vibe.model_hub_runtime import client as engine_client_module
from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter
from vibe.model_hub_runtime.client import EngineClient, EngineConnection
from vibe.model_hub_runtime.state import EngineStateStore
from vibe.model_hub_runtime.supervisor import EngineUnavailableError


BACKENDS = {
    "codex": ("responses", "openai_responses"),
    "claude": ("messages", "anthropic"),
    "opencode": ("chat/completions", "openai_chat"),
}


class FreshStore:
    """Every read reconstructs persisted data; failed writes cannot mutate it."""

    def __init__(self, config):
        self.persisted = copy.deepcopy(config.to_payload())
        self.write_error = False

    def load(self):
        return ModelHubConfig.from_payload(copy.deepcopy(self.persisted))

    def save(self, config):
        if self.write_error:
            if isinstance(self.write_error, BaseException):
                raise self.write_error
            raise OSError("fixture: read-only configuration")
        self.persisted = copy.deepcopy(config.to_payload())

    def mutate(self, operation):
        config = self.load()
        if operation(config):
            self.save(config)


def configured_service(tmp_path, **kwargs):
    service, clock = clock_service(tmp_path, **kwargs)
    models = _canonicalize_fixed_test_routes(service)
    models["opencode"] = "shared-model"
    return service, clock, models


@asynccontextmanager
async def gateway_client(service, backend, model):
    gateway = ModelHubTurnGateway(service)
    source = service.store.load().sources[0]
    base, token = await gateway.endpoint(
        backend, process_scope="lifecycle", turn_id="turn_lifecycle",
        requested_model_id=model, resolved_model_id="shared-model", source_id=source.id,
    )
    try:
        async with aiohttp.ClientSession(trust_env=False) as client:
            yield gateway, client, f"{base}/v1/{BACKENDS[backend][0]}", {"Authorization": f"Bearer {token}"}
    finally:
        await gateway.close()


@asynccontextmanager
async def loopback_engine(tmp_path, service, respond, *, models=None):
    """Use the production client/admission/stream observer against local HTTP."""
    config = service.store.load()
    state = EngineStateStore(tmp_path / "engine")
    for source in config.sources:
        source.credential_ref = state.store_api_key(
            "fixture", vendor=source.vendor, protocol=source.protocol, base_url=source.base_url,
        )
    service.store.save(config)
    state.sync_sources(service._bindings(config))
    app = web.Application()
    app.router.add_post("/{path:.*}", respond)
    if models is not None:
        app.router.add_get("/v1/models", models)
    runner = web.AppRunner(app, handler_cancellation=True)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    engine_client = EngineClient(
        EngineConnection(f"http://127.0.0.1:{runner.addresses[0][1]}", "fixture-management", "fixture-gateway"),
    )
    adapter = CLIProxyEngineAdapter(supervisor=SimpleNamespace(client=lambda: engine_client), state_store=state)
    adapter.ensure_installed = service.adapter.ensure_installed
    service.adapter = adapter
    service._engine_synced = True
    try:
        yield adapter, engine_client
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("stream", [False, True])
def test_unavailable_production_client_releases_unadmitted_half_open_claim(tmp_path, stream):
    """MH-RETRY-OWNERSHIP-001: completed engine-down never fires on_admitted."""
    async def run():
        service, clock, models = configured_service(tmp_path)
        source = service.store.load().sources[0]
        calls = []

        async def respond(request):
            calls.append(await request.json())
            return web.Response(body=WIRE["openai_responses"][3], content_type="application/json")

        def unavailable():
            raise EngineUnavailableError("fixture: no running engine client")

        async with loopback_engine(tmp_path, service, respond) as (adapter, engine_client):
            await fail(service, source)
            clock.advance(1)
            adapter.supervisor.client = unavailable
            observed = []
            snapshots = []
            with pytest.raises(ModelHubError, match="engine_down"):
                await service.resolve_with_recovery(
                    backend="codex", model_id=models["codex"], request={}, stream=stream,
                    attempt_observer=lambda *args: observed.append(args),
                    recovery_observer=snapshots.append,
                )
            assert observed == []  # Not an admitted request, and not another failed Source attempt.
            assert not any(snapshot and snapshot["attempt_count"] for snapshot in snapshots)
            assert adapter._active_transports == 0 and not service._mutation_lock.locked()
            entry = service.recovery._sources[source.id]
            assert entry.owner is None and entry.failures == 1
            assert service.agent_chain("codex", models["codex"])["chain"][0]["recovery"] == "eligible"
            # The next real request can acquire the same half-open admission.
            adapter.supervisor.client = lambda: engine_client
            async with gateway_client(service, "codex", models["codex"]) as (_gateway, client, url, headers):
                async with client.post(url, headers=headers, json={"model": "shared-model"}) as response:
                    assert response.status == 200
                    assert await response.read() == WIRE["openai_responses"][3]
            assert len(calls) == 1 and adapter._active_transports == 0
            assert source.id not in service.recovery._sources
    asyncio.run(run())


@pytest.mark.parametrize("stream", [False, True])
def test_an_unnamed_error_on_a_half_open_attempt_releases_the_claim_and_spares_the_source(tmp_path, stream):
    """Ambiguous evidence writes no Source health (decision 2026-10-11), and it must not keep the half-open slot."""
    async def run():
        service, clock, models = configured_service(tmp_path)
        source = service.store.load().sources[0]
        responses = [
            web.Response(
                status=200,
                content_type="text/event-stream",
                body=b'event: error\ndata: {"type":"error","code":"relay_specific_error",'
                b'"message":"upstream protocol error","param":null,"sequence_number":0}\n\n',
            )
            if stream
            else web.json_response({"error": {"type": "relay_specific_error", "message": "upstream protocol error"}}),
            web.Response(body=WIRE["openai_responses"][3], content_type="application/json"),
        ]

        async def respond(_request):
            return responses.pop(0)

        async with loopback_engine(tmp_path, service, respond):
            await fail(service, source)
            clock.advance(1)
            before = copy.deepcopy(service.store.load().to_payload())
            with pytest.raises(ModelHubError):
                await service.resolve_with_recovery(
                    backend="codex", model_id=models["codex"], request={}, stream=stream,
                )
            entry = service.recovery._sources[source.id]
            assert entry.owner is None and entry.failures == 1
            assert service.store.load().to_payload() == before
            assert service.agent_chain("codex", models["codex"])["chain"][0]["recovery"] == "eligible"
            # The next real request can acquire the same half-open admission.
            async with gateway_client(service, "codex", models["codex"]) as (_gateway, client, url, headers):
                async with client.post(url, headers=headers, json={"model": "shared-model"}) as response:
                    assert response.status == 200
                    assert await response.read() == WIRE["openai_responses"][3]
            assert responses == []
    asyncio.run(run())


@pytest.mark.parametrize("backend", ["codex", "claude"])
@pytest.mark.parametrize("diagnostic,delay", [
    ("503 server error", 30), ("429 rate limit", 60), ("quota exhausted", 300), ("connection failed", 0),
])
def test_native_failure_never_enters_http_recovery_streak(tmp_path, backend, diagnostic, delay):
    """MH-RETRY-NATIVE-002: native success has no Hub handle to clear a streak."""
    async def run():
        source = _source(
            "src_native001", "Native", channel="native_cli", status="active",
            vendor="anthropic" if backend == "claude" else "openai",
        )
        service, clock, models = configured_service(tmp_path, sources=[source])
        source.models[0].id = models[backend]
        service.store.config.agents[backend].routes[models[backend]] = ModelHubRouteConfig(
            hops=(ModelHubRouteHopConfig(source.id, models[backend]),),
        )
        router = ModelHubRuntimeRouter(
            service=service, native_cli_ready=lambda _backend: True, overlay_path=tmp_path / "overlay.json",
        )
        for _ in range(3):
            launch = await router.resolve(backend, models[backend])
            assert launch.channel == "native_cli"
            context = SimpleNamespace(platform_specific={})
            bind_launch(context, launch)
            assert await router.record_native_failure(context, diagnostic) is bool(delay)
            current = service.store.load().sources[0]
            if delay:
                assert current.state.retry_at == (clock.now() + timedelta(seconds=delay)).isoformat()
            assert service.recovery.annotations(service.store.load()) == {}
            assert service.recovery._sources == {}
            clock.advance(delay)
            assert service.agent_chain(backend, models[backend])["chain"][0]["runnable"]
        assert service.adapter.invocations == []
        assert not any(event["kind"] == "recover" for event in service.events.list())
    asyncio.run(run())


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("write_failure", ["io", "recovery_warning"])
def test_verified_response_survives_failed_recovery_persistence_and_fresh_reads(tmp_path, backend, stream, write_failure):
    """MH-RETRY-PERSISTENCE-001: stored cooldown cannot resurrect after recovery."""
    async def run():
        service, clock, models = configured_service(tmp_path)
        metadata, output, terminal, buffered = WIRE[BACKENDS[backend][1]]
        body = metadata + output + terminal if stream else buffered

        async def respond(_request):
            return web.Response(body=body, content_type="text/event-stream" if stream else "application/json")

        async with loopback_engine(tmp_path, service, respond) as (adapter, _engine_client):
            service.store = FreshStore(service.store.load())
            source = service.store.load().sources[0]
            await fail(service, source, "server_error")
            clock.advance(30)
            before = copy.deepcopy(service.store.persisted)
            service.store.write_error = True if write_failure == "io" else ValueError("fixture: recovery warnings")
            async with gateway_client(service, backend, models[backend]) as (_gateway, client, url, headers):
                async with client.post(url, headers=headers, json={"model": "shared-model", "stream": stream}) as response:
                    assert response.status == 200
                    assert await response.read() == body
            assert adapter._active_transports == 0
        assert service.store.persisted == before  # A fresh disk view still has the old cooldown.
        for _ in range(3):
            chain = service.agent_chain(backend, models[backend])
            assert chain["chain"][0]["health"] == "healthy"
            assert chain["chain"][0]["runnable"] is True
            assert "recovery" not in chain["chain"][0]
        assert sum(event["kind"] == "recover" for event in service.events.list()) == 1
        # A later real failure starts at the first delay, even if the stored old
        # cooldown could not be retired. No further caller loses its response.
        await fail(service, service.store.load().sources[0], "server_error")
        assert service.recovery._sources[source.id].failures == 1
        assert service.agent_chain(backend, models[backend])["supply_state"] == "waiting"
    asyncio.run(run())


@pytest.mark.parametrize("change", [
    "none", "new_deadline", "new_reason", "needs_action", "error", "credential", "verification",
    "endpoint", "deleted", "retired_model", "disk_cleared",
])
def test_recovered_disk_observation_is_exact_and_never_masks_new_authority(tmp_path, change):
    async def run():
        service, clock, models = configured_service(tmp_path)
        service.store = FreshStore(service.store.load())
        source = service.store.load().sources[0]
        await fail(service, source, "server_error")
        clock.advance(30)
        generation = service._reserve_settlement_generation(source.id)
        service.store.write_error = True
        await service._record_recovery_success(source.id, generation, backend="codex", model_id=models["codex"])
        assert service.agent_chain("codex", models["codex"])["chain"][0]["health"] == "healthy"
        config = service.store.load()
        current = config.sources[0]
        if change == "new_deadline":
            current.state.retry_at = (clock.now() + timedelta(seconds=60)).isoformat()
        elif change == "new_reason":
            current.state.detail_key = "models.source.cooldown.rate_limited"
        elif change in {"needs_action", "error"}:
            current.state = ModelHubSourceStateConfig(
                status=change,
                detail_key="models.source.error.unclassified" if change == "error"
                else "models.source.needs_action.credential_revoked",
            )
        elif change == "credential":
            current.credential_ref = "cred_replaced"
        elif change == "verification":
            ModelHubService._mark_source_unverified(current)
        elif change == "endpoint":
            current.base_url = "https://replacement.invalid"
        elif change == "deleted":
            config.sources = []
            for agent in config.agents.values():
                agent.sources.order = []
                agent.routes = {}
        elif change == "retired_model":
            current.models[0].retired = True
        elif change == "disk_cleared":
            current.state = ModelHubSourceStateConfig(status="standby")
        service.store.persisted = copy.deepcopy(config.to_payload())
        chain = service.agent_chain("codex", models["codex"])
        _assert_valid("agent-chain.schema.json", chain)
        if change == "deleted":
            assert chain["chain"] == [] and chain["supply_state"] == "interrupted"
            assert service.recovery._sources == service.recovery._retired_cooldowns == {}
            return
        hop = chain["chain"][0]
        if change in {"none", "disk_cleared"}:
            assert hop["health"] == "healthy" and hop["runnable"]
            assert "recovery" not in hop
        elif change == "new_deadline":
            assert hop["health"] == "cooldown" and not hop["runnable"]
            assert chain["supply_state"] == "waiting"
        elif change in {"new_reason", "credential", "verification", "endpoint"}:
            assert hop["recovery"] == "eligible"  # Not proven healthy for this new observation.
        else:
            assert not hop["runnable"] and chain["supply_state"] == "interrupted"
        # Re-delivery of the old success cannot retire a replacement identity.
        if change in {"credential", "verification", "endpoint"}:
            await service._record_recovery_success(source.id, generation, backend="codex", model_id=models["codex"])
            assert service.agent_chain("codex", models["codex"])["chain"][0]["recovery"] == "eligible"
        assert sum(event["kind"] == "recover" for event in service.events.list()) == 1
        if change == "disk_cleared":
            # Even the identical old payload is new once its retirement was
            # observed on disk; the tombstone is not a permanent Source flag.
            config.sources[0].state = replace(source.state, status="cooldown", retry_at=clock.now().isoformat(),
                                             detail_key="models.source.cooldown.server_error")
            service.store.persisted = config.to_payload()
            assert service.agent_chain("codex", models["codex"])["chain"][0]["recovery"] == "eligible"
    asyncio.run(run())


def test_recovered_annotation_cannot_retire_a_different_preview_cooldown(tmp_path):
    async def run():
        service, clock, models = configured_service(tmp_path)
        service.store = FreshStore(service.store.load())
        source = service.store.load().sources[0]
        await fail(service, source, "server_error")
        clock.advance(30)
        service.store.write_error = True
        await service._record_recovery_success(
            source.id, service._reserve_settlement_generation(source.id),
            backend="codex", model_id=models["codex"],
        )
        draft = service.store.load()
        draft.sources[0].state.retry_at = (clock.now() + timedelta(seconds=300)).isoformat()
        assert service._agent_chain(draft, "codex", models["codex"])["supply_state"] == "waiting"
        assert service.agent_chain("codex", models["codex"])["chain"][0]["health"] == "healthy"
    asyncio.run(run())


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("status,code,attempts", [(503, None, 3), (429, None, 2), (429, "quota_exhausted", 1)])
def test_failure_persistence_error_keeps_temporary_admission_and_original_terminal(tmp_path, backend, status, code, attempts):
    async def run():
        service, _clock, models = configured_service(tmp_path, outcomes=[
            _outcome(RawOutcomeKind.HTTP_ERROR, source_id="src_recovery01", status=status, code=code)
            for _ in range(attempts)
        ])
        service.store = FreshStore(service.store.load())
        before = copy.deepcopy(service.store.persisted)
        service.store.write_error = True
        reason = code or ("server_error" if status == 503 else "rate_limited")
        async with gateway_client(service, backend, models[backend]) as (gateway, client, url, headers):
            async with client.post(url, headers=headers, json={"model": "shared-model"}) as response:
                assert response.status == (400 if backend == "codex" else 424)
                ending = (await response.json())["error"]
                # The ending names the failure recovery waited out, whichever way the window closed.
                assert (ending["code"], ending["cause"]) == (RECOVERY_EXHAUSTED_CODE, reason)
            projection = gateway.correlation.terminal_projection("turn_lifecycle", backend=backend)
            if backend == "opencode":
                assert projection is None  # Shared-server HTTP does not prove an exact Turn.
            else:
                assert projection is not None
                assert [blocker.reason for blocker in projection.supply_facts.blockers] == [reason]
                gateway.correlation.settle("turn_lifecycle", settled_by=SETTLED_BY_TERMINAL_RESULT)
                record = service.provenance.get("turn_lifecycle")
                assert [attempt["http_status"] for attempt in record["failed_attempts"]] == [status] * attempts
        assert len(service.adapter.invocations) == attempts
        assert service.store.persisted == before
        chain = service.agent_chain(backend, models[backend])
        assert chain["supply_state"] == "waiting"
        assert chain["chain"][0]["health"] == "cooldown"
        _assert_valid("agent-chain.schema.json", chain)
        with pytest.raises(ModelHubError) as terminal:
            await service.resolve(backend=backend, model_id=models[backend], request={})
        assert [blocker.reason for blocker in terminal.value.blockers] == ["cooldown"]
    asyncio.run(run())


def test_a_usage_cap_cools_its_source_as_quota_until_the_upstream_reset(tmp_path):
    """MH-RETRY-ADVICE-002: a subscription's usage cap waits for its own reset, not a 60 s rate limit."""
    async def run():
        service, clock, models = configured_service(tmp_path)
        # A ChatGPT subscription past its weekly cap, as the engine relays it:
        # the upstream body unchanged and no Retry-After header.
        resets_in = 475224
        resets_at = int(time.time()) + resets_in
        body = json.dumps({"error": {
            "type": "usage_limit_reached", "message": "The usage limit has been reached",
            "plan_type": "prolite", "resets_at": resets_at, "eligible_promo": None,
            "limit_window_minutes": 10080, "resets_in_seconds": resets_in,
        }}, separators=(",", ":"))
        calls = []

        async def respond(request):
            calls.append(await request.json())
            return web.Response(status=429, text=body, content_type="application/json")

        async with loopback_engine(tmp_path, service, respond):
            async with gateway_client(service, "codex", models["codex"]) as (_gateway, client, url, headers):
                async with client.post(url, headers=headers, json={"model": "shared-model"}) as response:
                    ending = (await response.json())["error"]
                    assert (ending["code"], ending["cause"]) == (RECOVERY_EXHAUSTED_CODE, "quota_exhausted")
        state = service.store.load().sources[0].state
        assert (state.status, state.detail_key) == ("cooldown", "models.source.cooldown.quota_exhausted")
        assert state.retry_at == datetime.fromtimestamp(resets_at, timezone.utc).isoformat()
        # The cap is not probed again inside the turn's recovery window.
        assert len(calls) == 1 and clock.delays == []
    asyncio.run(run())


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("backend", ["claude", "codex"])
@pytest.mark.parametrize("windows,advice,expected", [
    ({"5h": 18000}, {}, 18000),
    ({"7d": 604800}, {}, 604800),
    ({"5h": 18000, "7d": 604800}, {"Retry-After": "60"}, 604800),
    ({"5h": 18000}, {"Retry-After": "24000", "Retry-After-Ms": "25000000"}, 25000),
    ({"5h": 18000}, {"Retry-After": "25000", "Retry-After-Ms": "24000000"}, 25000),
    ({"5h": 20}, {}, 300),  # A short upstream reset cannot shorten the quota ladder.
])
def test_claude_cap_headers_drive_persisted_recovery_without_early_probes(
    tmp_path, monkeypatch, caplog, stream, backend, windows, advice, expected,
):
    """MH-RETRY-ADVICE-004: shared Claude caps survive settlement/restart without short-ladder probes or raw headers."""
    async def run():
        source = _source("src_claudecap", "Claude", vendor="anthropic", protocol="anthropic")
        service, clock, models = configured_service(tmp_path, sources=[source])
        receipt = clock.now()
        deadline = receipt + timedelta(seconds=expected)

        class ReceiptClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock.now()

        monkeypatch.setattr(engine_client_module, "datetime", ReceiptClock)
        raw_headers = {
            "aNtHrOpIc-Ratelimit-Unified-Status": "rejected",
            "Authorization": "private-upstream-credential",
            "Set-Cookie": "private-upstream-cookie",
            "Organization-Id": "private-upstream-org",
            "Anthropic-Ratelimit-Unified-Unknown": "private-unknown-value",
            **advice,
        }
        for window, delay in windows.items():
            raw_headers[f"Anthropic-Ratelimit-Unified-{window}-Status"] = "rejected"
            raw_headers[f"Anthropic-Ratelimit-Unified-{window}-Reset"] = str(int(receipt.timestamp()) + delay)
        calls = []

        async def respond(request):
            calls.append(await request.json())
            if clock.now() < deadline:
                return web.json_response({"error": {"type": "rate_limit_error", "message": "Rate limited"}},
                                         status=429, headers=raw_headers)
            metadata, output, terminal, buffered = WIRE[BACKENDS[backend][1]]
            return web.Response(body=metadata + output + terminal if stream else buffered,
                                content_type="text/event-stream" if stream else "application/json")

        async with loopback_engine(tmp_path, service, respond):
            service.store = FreshStore(service.store.load())
            async with gateway_client(service, backend, models[backend]) as (gateway, client, url, headers):
                async with client.post(url, headers=headers, json={"model": "shared-model", "stream": stream}) as response:
                    body = await response.json()
                    assert body["error"]["cause"] == "quota_exhausted"
                    assert body["error"]["code"] == RECOVERY_EXHAUSTED_CODE
                    assert not any(name.lower() in response.headers for name in raw_headers)
                gateway.correlation.settle("turn_lifecycle", settled_by=SETTLED_BY_TERMINAL_RESULT)
                record = service.provenance.get("turn_lifecycle")
                assert record["failed_attempts"] == [{
                    "source_id": source.id, "configured_model_id": "shared-model", "channel": "hub",
                    "reason": "quota_exhausted", "http_status": 429, "upstream_detail": "Rate limited",
                }]
            stored = service.store.load().sources[0].state
            assert (stored.status, stored.detail_key, stored.retry_at) == (
                "cooldown", "models.source.cooldown.quota_exhausted", deadline.isoformat(),
            )
            assert len(calls) == 1 and clock.delays == []
            assert service.events.list()[0]["reason"] == "quota_exhausted"
            surfaces = json.dumps([
                body, record, service.list_events(), service.list_sources(), service.store.persisted, caplog.text,
            ])
            assert "anthropic-ratelimit" not in surfaces.lower()
            for secret in ("private-upstream-credential", "private-upstream-cookie", "private-upstream-org", "private-unknown-value"):
                assert secret not in surfaces

            # A new policy restores eligibility from fresh persisted config,
            # never from this request's header projection or live retry streak.
            service.recovery = clock.policy()
            clock.advance(expected - 121)
            with pytest.raises(ModelHubError):
                await service.resolve_with_recovery(backend=backend, model_id=models[backend], request={})
            assert len(calls) == 1 and clock.delays == []
            chain = service.agent_chain(backend, models[backend])
            assert chain["supply_state"] == "waiting"
            assert chain["chain"][0]["retry_at"] == deadline.isoformat()
            assert chain["chain"][0]["runnable"] is False
            _assert_valid("agent-chain.schema.json", chain)
            # A concurrent later-completing short failure cannot pull it forward.
            await fail(service, service.store.load().sources[0], "rate_limited")
            assert service.store.load().sources[0].state.retry_at == deadline.isoformat()
            assert service.store.load().sources[0].state.detail_key == "models.source.cooldown.quota_exhausted"
            clock.advance(121)
            assert service.store.load().sources[0].state.status == "cooldown"
            async with gateway_client(service, backend, models[backend]) as (_gateway, client, url, headers):
                async with client.post(url, headers=headers, json={"model": "shared-model", "stream": stream}) as response:
                    assert response.status == 200
                    await response.read()
            assert len(calls) == 2
            assert service.store.load().sources[0].state.status == "standby"
    asyncio.run(run())


@pytest.mark.parametrize("engine_refuses", [True, False], ids=["engine", "upstream"])
def test_a_401_the_engine_itself_gave_spares_the_source(tmp_path, engine_refuses):
    """MH-RUNTIME-015: the engine's own 401 is told from an upstream's by a witness no upstream answers (#2755).

    The bodies are identical, as an upstream JSON error passes through the
    engine unchanged. Only the engine-local listing refusing the same key makes
    the 401 the engine's: the turn ends as the engine being down, the Source
    keeps its health, and the next admission checks the engine again.
    """
    async def run():
        service, _clock, models = configured_service(tmp_path)
        before = service.store.load().sources[0].state

        async def respond(request):
            await request.read()
            return web.json_response({"error": "Invalid API key"}, status=401)

        async def listing(_request):
            if engine_refuses:
                return web.json_response({"error": "Invalid API key"}, status=401)
            return web.json_response({"object": "list", "data": []})

        rechecks = []
        async with loopback_engine(tmp_path, service, respond, models=listing) as (adapter, _engine):
            adapter.supervisor.recheck_engine = lambda: rechecks.append(True)
            async with gateway_client(service, "codex", models["codex"]) as (_gateway, client, url, headers):
                async with client.post(url, headers=headers, json={"model": "shared-model"}) as response:
                    assert response.status >= 400
        state = service.store.load().sources[0].state
        if engine_refuses:
            assert state == before
            assert rechecks == [True]
        else:
            assert (state.status, state.detail_key) == ("needs_action", "models.source.needs_action.credential_revoked")
            assert rechecks == []
    asyncio.run(run())


_SPEND_LIMIT = ("needs_action", "models.source.needs_action.spend_limit_reached")


@pytest.mark.parametrize(("error", "settled"), [
    # Codex's own client turns this 429 type into "upgrade to Plus" (openai/codex
    # codex-api/src/api_bridge.rs); only a plan change ends it.
    (
        {"type": "usage_not_included", "message": "To use Codex with your ChatGPT plan, upgrade to Plus."},
        ("needs_action", "models.source.needs_action.plan_excludes_product"),
    ),
    # OpenAI's error-code guide: a billing 429 names its cause in the code, the
    # type may stay the generic insufficient_quota, and retrying restores none.
    (
        {"type": "insufficient_quota", "code": "credit_balance_exhausted", "message": "No prepaid credits remain."},
        ("needs_action", "models.source.needs_action.balance_exhausted"),
    ),
    ({"type": "insufficient_quota", "code": "organization_spend_limit_exceeded", "message": "Spend limit."}, _SPEND_LIMIT),
    ({"type": "insufficient_quota", "code": "project_spend_limit_exceeded", "message": "Spend limit."}, _SPEND_LIMIT),
    ({"type": "insufficient_quota", "code": "organization_usage_limit_exceeded", "message": "Usage limit."}, _SPEND_LIMIT),
    # A request-class type beside the code does not decide it.
    ({"type": "invalid_request_error", "code": "project_spend_limit_exceeded", "message": "Spend limit."}, _SPEND_LIMIT),
    # The generic type alone names no cause, so it keeps cooling as quota.
    (
        {"type": "insufficient_quota", "message": "You exceeded your current quota."},
        ("cooldown", "models.source.cooldown.quota_exhausted"),
    ),
], ids=lambda value: value.get("code", value.get("type")) if isinstance(value, dict) else None)
def test_a_plan_or_billing_429_settles_its_source_by_the_code_that_names_it(tmp_path, error, settled):
    """MH-RETRY-ADVICE-003: a 429 naming a plan or billing cause is not a 60 s rate limit (#2530)."""
    async def run():
        service, _clock, models = configured_service(tmp_path)
        body = json.dumps({"error": error}, separators=(",", ":"))
        calls = []

        async def respond(request):
            calls.append(await request.json())
            return web.Response(status=429, text=body, content_type="application/json")

        async with loopback_engine(tmp_path, service, respond):
            async with gateway_client(service, "codex", models["codex"]) as (_gateway, client, url, headers):
                async with client.post(url, headers=headers, json={"model": "shared-model"}) as response:
                    assert response.status >= 400
        state = service.store.load().sources[0].state
        assert (state.status, state.detail_key) == settled
        assert (state.retry_at is None) is (settled[0] == "needs_action")
        assert len(calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize("persisted", [False, True])
@pytest.mark.parametrize("reason,delay", [
    ("server_error", 30), ("rate_limited", 60), ("quota_exhausted", 300), ("network", 1),
])
@pytest.mark.parametrize("transition", ["none", "needs_action", "error", "retired_model", "recovered"])
def test_terminal_supply_projection_uses_effective_transient_facts_and_stronger_blockers(
    tmp_path, persisted, reason, delay, transition,
):
    async def run():
        service, clock, models = configured_service(tmp_path)
        service.store = FreshStore(service.store.load())
        service.store.write_error = not persisted
        source = service.store.load().sources[0]
        await fail(service, source, reason)
        config = service.store.load()
        if transition in {"needs_action", "error"}:
            config.sources[0].state = ModelHubSourceStateConfig(
                status=transition,
                detail_key="models.source.error.unclassified" if transition == "error"
                else "models.source.needs_action.credential_revoked",
            )
        elif transition == "retired_model":
            config.sources[0].models[0].retired = True
        service.store.persisted = config.to_payload()
        if transition == "recovered":
            clock.advance(delay)
            service.store.write_error = True
            await service._record_recovery_success(
                source.id, service._reserve_settlement_generation(source.id),
                backend="codex", model_id=models["codex"],
            )
            chain = service.agent_chain("codex", models["codex"])
            assert chain["supply_state"] == "ok" and chain["chain"][0]["health"] == "healthy"
            _config, resolution = service._inspect_terminal_chain(backend="codex", model_id=models["codex"])
            assert resolution.candidate_hops[0].cooldown_reason is None
            return
        with pytest.raises(ModelHubError) as terminal:
            await service.resolve(backend="codex", model_id=models["codex"], request={})
        expected = {
            "none": reason, "needs_action": "credential_revoked",
            "error": "unclassified_error", "retired_model": "model_unsupported",
        }[transition]
        facts = terminal.value.turn_outcome.supply_facts
        assert [blocker.reason for blocker in facts.blockers] == [expected]
        assert facts.supply_state == ("waiting" if transition == "none" else "interrupted")
        assert bool(facts.retry_at) is (transition == "none")
        assert [blocker.reason for blocker in terminal.value.blockers] == [
            ("network" if reason == "network" else "cooldown") if transition == "none" else expected,
        ]
    asyncio.run(run())


def test_persisted_cooldown_without_failure_evidence_does_not_invent_server_error(tmp_path):
    async def run():
        service, clock, models = configured_service(tmp_path)
        source = service.store.load().sources[0]
        source.state = ModelHubSourceStateConfig(
            status="cooldown", retry_at=(clock.now() + timedelta(seconds=30)).isoformat(),
        )
        with pytest.raises(ModelHubError) as terminal:
            await service.resolve(backend="codex", model_id=models["codex"], request={})
        assert [blocker.reason for blocker in terminal.value.blockers] == ["cooldown"]
        assert terminal.value.turn_outcome.supply_facts.blockers == ()
    asyncio.run(run())


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("wake", ["owner_success", "runnable_edit", "blocked_edit"])
def test_expiry_remains_a_closed_terminal_when_live_supply_changes(tmp_path, backend, wake):
    """MH-RETRY-EXPIRY-001: an exhausted waiter is not a fresh route failure."""
    async def run():
        service, clock, models = configured_service(tmp_path)
        source = service.store.load().sources[0]
        await fail(service, source)
        clock.advance(1)
        generation = service._reserve_settlement_generation(source.id)
        assert service.recovery.claim(source, generation)
        entered, release = asyncio.Event(), asyncio.Event()

        async def held_wait(_seconds):
            entered.set()
            await release.wait()

        service.recovery.sleep = held_wait
        async with gateway_client(service, backend, models[backend]) as (_gateway, client, url, headers):
            task = asyncio.create_task(client.post(url, headers=headers, json={"model": "shared-model"}))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                clock.advance(121)
                if wake == "owner_success":
                    await service._record_recovery_success(
                        source.id, generation, backend=backend, model_id=models[backend],
                    )
                elif wake == "runnable_edit":
                    source.credential_ref = "cred_replaced"
                    service.recovery.reconcile(service.store.load())
                else:
                    source.state = ModelHubSourceStateConfig(
                        status="needs_action", detail_key="models.source.needs_action.credential_revoked",
                    )
                release.set()
                response = await asyncio.wait_for(task, 2)
                assert response.status == (400 if backend == "codex" else 424)
                assert "Retry-After" not in response.headers
                assert await response.json() == {
                    "type": "error",
                    "error": {
                        "type": RECOVERY_EXHAUSTED_CODE, "code": RECOVERY_EXHAUSTED_CODE,
                        "message": RECOVERY_EXHAUSTED_MESSAGE, "cause": "network",
                    },
                }
                assert service.adapter.invocations == []
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("change", ["none", "owner_success", "blocked_edit"])
def test_window_expiry_during_engine_preparation_preserves_admission_cause(tmp_path, backend, change):
    async def run():
        service, clock, models = configured_service(tmp_path)
        source = service.store.load().sources[0]
        await fail(service, source)
        original_prepare = service._prepare_engine_for_demand

        async def expire_during_preparation(**kwargs):
            clock.advance(121)
            if change == "owner_success":
                await service._record_recovery_success(
                    source.id, service._latest_source_attempt_generation[source.id],
                    backend=backend, model_id=models[backend],
                )
            elif change == "blocked_edit":
                source.state = ModelHubSourceStateConfig(
                    status="needs_action", detail_key="models.source.needs_action.credential_revoked",
                )
            await original_prepare(**kwargs)

        service._prepare_engine_for_demand = expire_during_preparation
        async with gateway_client(service, backend, models[backend]) as (gateway, client, url, headers):
            async with client.post(url, headers=headers, json={"model": "shared-model"}) as response:
                assert response.status == (400 if backend == "codex" else 424)
                assert (await response.json())["error"]["code"] == RECOVERY_EXHAUSTED_CODE
                assert "Retry-After" not in response.headers
            projection = gateway.correlation.terminal_projection("turn_lifecycle", backend=backend)
            if backend == "opencode":
                assert projection is None
            else:
                # The wire retains admission expiry while the canonical
                # projection still reports current supply, not a stale outage.
                assert projection.supply_facts.supply_state == (
                    "interrupted" if change == "blocked_edit" else "waiting"
                )
                assert [blocker.reason for blocker in projection.supply_facts.blockers] == (
                    ["credential_revoked"] if change == "blocked_edit" else []
                )
        assert clock.delays == [1] and service.adapter.invocations == []
        assert not service._mutation_lock.locked()
    asyncio.run(run())


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("ending", ["temporary", "invalid", "auth", "engine", "post_output"])
def test_admitted_terminal_classification_is_not_replaced_by_elapsed_window(tmp_path, backend, ending):
    async def run():
        service, clock, models = configured_service(tmp_path)
        metadata, output, _terminal, _buffered = WIRE[BACKENDS[backend][1]]
        calls = []

        async def respond(request):
            calls.append(await request.json())
            if len(calls) == 1:
                return web.json_response({"error": {"type": "server_error"}}, status=503)
            clock.advance(121)  # A connected inference may outlive admission.
            if ending == "post_output":
                error = b'data: {"type":"error","error":{"type":"server_error","message":"fixture"}}\n\n'
                return web.Response(body=metadata + output + error, content_type="text/event-stream")
            status, code = {
                "temporary": (503, "server_error"),
                "invalid": (400, "invalid_request_error"),
                "auth": (403, "invalid_api_key"),
                "engine": (502, "engine_down"),
            }[ending]
            return web.json_response({"error": {"type": code}}, status=status)

        async with loopback_engine(tmp_path, service, respond) as (adapter, engine_client):
            if ending == "engine":
                client_reads = []

                def client_or_late_engine_failure():
                    client_reads.append(True)
                    if len(client_reads) == 1:
                        return engine_client
                    clock.advance(121)
                    raise EngineUnavailableError("fixture: engine disappeared before admission")

                adapter.supervisor.client = client_or_late_engine_failure
            async with gateway_client(service, backend, models[backend]) as (_gateway, client, url, headers):
                async with client.post(
                    url, headers=headers, json={"model": "shared-model", "stream": ending == "post_output"},
                ) as response:
                    body = await response.read()
                    if ending == "temporary":
                        assert response.status == (400 if backend == "codex" else 424)
                        assert RECOVERY_EXHAUSTED_CODE.encode() in body
                    else:
                        assert RECOVERY_EXHAUSTED_CODE.encode() not in body
                        if ending == "post_output":
                            assert response.status == 200 and output in body
                        elif ending == "invalid":
                            assert response.status == 400
            assert adapter._active_transports == 0
        assert len(calls) == (1 if ending == "engine" else 2) and clock.delays == [30]
    asyncio.run(run())


@pytest.mark.parametrize("backend", BACKENDS)
def test_expiry_after_mixed_real_failures_keeps_the_action_blocker_terminal(tmp_path, backend):
    """MH-RETRY-EXPIRY-002: elapsed time cannot erase an admitted hard failure."""
    async def run():
        first, second = _source("src_primary01", "Primary"), _source("src_backup001", "Backup")
        service, clock, models = configured_service(tmp_path, sources=[first, second], outcomes=[
            _outcome(RawOutcomeKind.NETWORK_ERROR, source_id=first.id),
            _outcome(RawOutcomeKind.NETWORK_ERROR, source_id=second.id),
            _outcome(RawOutcomeKind.HTTP_ERROR, source_id=first.id, status=403),
            _outcome(RawOutcomeKind.HTTP_ERROR, source_id=second.id, status=503),
        ])
        original_invoke = service.adapter.invoke
        admissions = []

        async def late_last_attempt(*args, **kwargs):
            on_admitted = kwargs["on_admitted"]

            def admitted():
                admissions.append((args[0], args[1]))
                on_admitted()

            kwargs["on_admitted"] = admitted
            handle = await original_invoke(*args, **kwargs)
            if len(service.adapter.invocations) == 4:
                clock.advance(121)
            return handle

        service.adapter.invoke = late_last_attempt
        async with gateway_client(service, backend, models[backend]) as (gateway, client, url, headers):
            async with client.post(url, headers=headers, json={"model": "shared-model"}) as response:
                assert response.status == 503
                payload = await response.json()
                assert payload["error"]["code"] == payload["error"]["type"] == "mapping_target_unavailable"
            projection = gateway.correlation.terminal_projection("turn_lifecycle", backend=backend)
            if backend == "opencode":
                assert projection is None
                assert service.provenance.get("turn_lifecycle") is None
            else:
                assert projection is not None and projection.outcome == "exhausted"
                assert projection.supply_facts.supply_state == "interrupted"
                assert [(blocker.source, blocker.reason) for blocker in projection.supply_facts.blockers] == [
                    ("Primary", "credential_revoked"), ("Backup", "server_error"),
                ]
                gateway.correlation.settle("turn_lifecycle", settled_by=SETTLED_BY_TERMINAL_RESULT)
                record = service.provenance.get("turn_lifecycle")
                assert record["outcome"] == "exhausted"
                assert record["failed_attempts"] == [
                    {"source_id": source_id, "configured_model_id": "shared-model", "channel": "hub",
                     "reason": reason, **({"http_status": status} if status else {})}
                    for source_id, reason, status in [
                        (first.id, "network", None), (second.id, "network", None),
                        (first.id, "credential_revoked", 403), (second.id, "server_error", 503),
                    ]
                ]
        assert len(service.adapter.invocations) == 4 and clock.delays == [1]
        assert admissions == [(source_id, "shared-model") for source_id in [first.id, second.id] * 2]
        assert service.agent_chain(backend, models[backend])["supply_state"] == "interrupted"
    asyncio.run(run())


@pytest.mark.parametrize("backend", ["codex", "claude"])
def test_waiters_without_an_attempt_record_blocked_supply_not_a_protocol_error(tmp_path, backend):
    """MH-RETRY-PROVENANCE-001: losing the half-open slot is blocked supply, not a gateway fault."""
    async def run():
        service, clock, models = configured_service(tmp_path)
        service.store = FreshStore(service.store.load())
        source = service.store.load().sources[0]
        await fail(service, source, "server_error")
        clock.advance(31)
        claim = service.recovery.claim
        peers = []

        def a_peer_claims_first(candidate, generation):
            # Another waiter wins the eligible slot between inspection and claim.
            if not peers:
                peers.append(claim(candidate, service._reserve_settlement_generation(candidate.id)))
            return claim(candidate, generation)

        service.recovery.claim = a_peer_claims_first
        async with gateway_client(service, backend, models[backend]) as (gateway, client, url, headers):
            async with client.post(url, headers=headers, json={"model": "shared-model"}) as response:
                assert response.status == (400 if backend == "codex" else 424)
            gateway.correlation.settle("turn_lifecycle", settled_by=SETTLED_BY_TERMINAL_RESULT)
            record = service.provenance.get("turn_lifecycle")
        assert peers == [True] and service.adapter.invocations == []
        assert (record["outcome"], record["model_supply_state"]) == ("no_candidate", "waiting")
        assert record["terminal_error"] is None and record["failed_attempts"] == []
    asyncio.run(run())


@pytest.mark.parametrize("backend", ["codex", "claude"])
def test_a_waiter_ending_beside_the_slot_owner_leaves_no_turn_projection(tmp_path, backend):
    """MH-RETRY-PROVENANCE-003: the gateway commits a peer-scoped ending only for its own request."""
    async def run():
        service, clock, models = configured_service(tmp_path, outcomes=[
            _outcome(RawOutcomeKind.SUCCESS, source_id="src_recovery01"),
        ])
        source = service.store.load().sources[0]
        await fail(service, source, "server_error")
        clock.advance(31)
        release = asyncio.Event()
        original_invoke = service.adapter.invoke

        async def owner_waits_upstream(*args, **kwargs):
            handle = await original_invoke(*args, **kwargs)
            await release.wait()
            return handle

        service.adapter.invoke = owner_waits_upstream
        async with gateway_client(service, backend, models[backend]) as (gateway, client, url, headers):
            async def post():
                async with client.post(url, headers=headers, json={"model": "shared-model"}) as response:
                    return response.status

            owner = asyncio.create_task(post())
            while not service.adapter.invocations:
                await asyncio.sleep(0.01)
            assert await post() == (400 if backend == "codex" else 424)
            release.set()
            assert await owner == 200
            assert gateway.correlation.terminal_projection("turn_lifecycle", backend=backend) is None
            gateway.correlation.settle("turn_lifecycle", settled_by=SETTLED_BY_TERMINAL_RESULT)
            record = service.provenance.get("turn_lifecycle")
            assert record["outcome"] == "served" and record["model_supply_state"] is None
    asyncio.run(run())


KEEPALIVE = b": keepalive\n\n"


def test_claude_stream_rejected_within_its_first_byte_budget_keeps_the_status(tmp_path):
    """MH-RUNTIME-012: a rejection after a few seconds still reaches Claude as HTTP 400.

    Claude Code downgrades an unsupported request feature (a beta, or
    thinking.display) and retries only on an HTTP 400 naming it; a rejection
    inside a committed 200 stream ends the turn instead.
    """
    rejection = "thinking.adaptive.display: Input should be 'summarized', 'omitted'"

    async def run() -> None:
        source = _source("src_rejects1", "Rejects", vendor="anthropic", protocol="anthropic")
        service, _clock, models = configured_service(tmp_path, sources=[source])
        calls = []

        async def respond(request: web.Request) -> web.Response:
            calls.append(await request.json())
            # Slower than a fast Source, well within Claude's first-byte window.
            await asyncio.sleep(2.5)
            return web.json_response(
                {"type": "error", "error": {"type": "invalid_request_error", "message": rejection}}, status=400,
            )

        async with loopback_engine(tmp_path, service, respond) as (adapter, _engine_client):
            async with gateway_client(service, "claude", models["claude"]) as (_gateway, client, url, headers):
                async with client.post(
                    url, headers=headers, json={"model": "shared-model", "stream": True},
                ) as response:
                    body = await response.text()
                    assert response.status == 400, body
                    assert rejection in json.loads(body)["error"]["message"]
            assert adapter._active_transports == 0
        assert len(calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("ending", ["served", "recovered", "exhausted", "disconnect"])
def test_slow_stream_resolution_keeps_claude_connected(monkeypatch, tmp_path, backend, ending):
    """MH-RUNTIME-010: Claude sees headers and keepalives while the Hub resolves.

    Claude Code aborts a stream whose headers or bytes stay silent past its
    watchdogs, so its streams commit early and carry every ending inside the
    stream. Codex and OpenCode key recovery on the HTTP status and keep it.
    """
    monkeypatch.setattr(turn_gateway, "_EARLY_STREAM_COMMIT_SECONDS", 0.05)
    monkeypatch.setattr(turn_gateway, "_STREAM_KEEPALIVE_SECONDS", 0.02)
    exhausted_event = b"event: error\ndata: " + json.dumps(
        {"type": "error", "error": {"type": "api_error", "message": RECOVERY_EXHAUSTED_MESSAGE}},
        separators=(",", ":"),
    ).encode() + b"\n\n"

    async def run() -> None:
        endpoint, protocol = BACKENDS[backend]
        source = _source(
            "src_patient1", "Patient", vendor="anthropic" if backend == "claude" else "openai", protocol=protocol,
        )
        service, clock, models = configured_service(tmp_path, sources=[source])
        metadata, output, terminal, _buffered = WIRE[protocol]
        held, release, upstream_cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []

        async def respond(request: web.Request) -> web.Response:
            calls.append(await request.json())
            if len(calls) == 1:
                # An upstream that sends nothing until its first model output.
                held.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    upstream_cancelled.set()
                    raise
            elif ending == "exhausted":
                clock.advance(121)
            if ending == "served" or (ending == "recovered" and len(calls) == 2):
                return web.Response(body=metadata + output + terminal, content_type="text/event-stream")
            return web.json_response({"error": {"type": "server_error"}}, status=503)

        try:
            async with loopback_engine(tmp_path, service, respond) as (adapter, _engine_client):
                async with gateway_client(service, backend, models[backend]) as (_gateway, client, url, headers):
                    post = asyncio.create_task(
                        client.post(url, headers=headers, json={"model": "shared-model", "stream": True}),
                    )
                    try:
                        await asyncio.wait_for(held.wait(), timeout=2)
                        if backend == "claude":
                            response = await asyncio.wait_for(post, timeout=2)
                            assert response.status == 200
                            assert response.content_type == "text/event-stream"
                            first = response.content.readexactly(len(KEEPALIVE))
                            assert await asyncio.wait_for(first, timeout=2) == KEEPALIVE
                        else:
                            done, _pending = await asyncio.wait({post}, timeout=0.2)
                            assert not done, "a status-keyed stream committed before resolution ended"
                        if ending == "disconnect":
                            if backend == "claude":
                                response.close()
                            else:
                                post.cancel()
                            await asyncio.wait_for(upstream_cancelled.wait(), timeout=2)
                        else:
                            release.set()
                            if backend != "claude":
                                response = await asyncio.wait_for(post, timeout=2)
                            body = await asyncio.wait_for(response.read(), timeout=2)
                            while body.startswith(KEEPALIVE):
                                body = body[len(KEEPALIVE):]
                            if ending != "exhausted":
                                assert response.status == 200
                                assert body == metadata + output + terminal
                            elif backend == "claude":
                                assert response.status == 200
                                assert body == exhausted_event
                            else:
                                assert response.status == (400 if backend == "codex" else 424)
                                assert RECOVERY_EXHAUSTED_CODE.encode() in body
                    finally:
                        if not post.done():
                            post.cancel()
                        await asyncio.gather(post, return_exceptions=True)
                assert adapter._active_transports == 0
        finally:
            release.set()
        assert len(calls) == (1 if ending in {"served", "disconnect"} else 2)
        assert clock.delays == ([] if ending in {"served", "disconnect"} else [30])

    asyncio.run(run())


def _large_output(protocol: str) -> bytes:
    """One model-output frame carrying 1 MiB of text in the protocol's shape."""

    text = "x" * (1 << 20)
    if protocol == "anthropic":
        payload = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}
        return b"event: content_block_delta\ndata: " + json.dumps(payload).encode() + b"\n\n"
    payload = {"type": "response.output_text.delta", "delta": text}
    return b"event: response.output_text.delta\ndata: " + json.dumps(payload).encode() + b"\n\n"


IDLE_BOUNDED_STALLS = ("before_headers", "before_output", "after_output", "heartbeat", "backpressure")
# Large enough to fill every buffer between the engine and a caller that stops reading.
BACKPRESSURE_FRAMES = 24


@pytest.mark.parametrize(
    "backend,vendor,source_protocol,stall",
    [
        *(
            (backend, vendor, BACKENDS[backend][1], stall)
            for backend, vendor in (("codex", "openai"), ("claude", "anthropic"))
            for stall in IDLE_BOUNDED_STALLS
        ),
        # Healthy streams that send nothing for minutes: xAI sends a whole tool
        # call at once, the engine's translators drop the upstream heartbeat,
        # and Chat has no measured cadence.
        ("codex", "xai", "openai_responses", "unbounded"),
        ("codex", "anthropic", "anthropic", "unbounded"),
        ("claude", "openai", "openai_responses", "unbounded"),
        ("opencode", "openai", "openai_chat", "unbounded"),
    ],
)
def test_a_silent_engine_stream_is_cancelled_and_retried_at_its_idle_bound(
    monkeypatch, tmp_path, backend, vendor, source_protocol, stall,
):
    """MH-RUNTIME-013: a stream silent past the idle bound ends there, not at the caller's bound.

    The engine sets no timeout once its upstream is connected, so a relay that
    stays alive while its own upstream is stuck sends nothing until the caller
    gives up. The gateway cancels that engine request at its idle bound: before
    model output it retries without the caller seeing anything; after output it
    ends the stream inside the protocol. Any byte, such as an Anthropic ping,
    keeps a slow stream alive, and a caller that stops reading pauses the bound.
    """
    bound = 0.3
    monkeypatch.setattr(engine_client_module, "_STREAM_IDLE_TIMEOUT_SECONDS", bound)

    async def run() -> None:
        _endpoint, protocol = BACKENDS[backend]
        source = _source("src_silent01", "Silent", vendor=vendor, protocol=source_protocol)
        service, clock, models = configured_service(tmp_path, sources=[source])
        metadata, output, terminal, _buffered = WIRE[protocol]
        if stall == "backpressure":
            output = _large_output(protocol) * BACKPRESSURE_FRAMES
        calls: list[dict] = []
        stalled_for: list[float] = []

        async def respond(request: web.Request) -> web.StreamResponse:
            calls.append(await request.json())
            stalls = len(calls) == 1 and stall not in {"heartbeat", "backpressure"}
            started = time.monotonic()
            try:
                if stalls and stall == "before_headers":
                    await asyncio.Event().wait()
                response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
                await response.prepare(request)
                await response.write(metadata)
                if stall == "heartbeat":
                    for _ in range(4):
                        await asyncio.sleep(bound / 2)
                        await response.write(b": keep-alive\n\n")
                if stall == "unbounded":
                    await asyncio.sleep(bound * 3)
                if stalls and stall == "before_output":
                    started = time.monotonic()
                    await asyncio.Event().wait()
                await response.write(output)
                if stalls and stall == "after_output":
                    started = time.monotonic()
                    await asyncio.Event().wait()
                await response.write(terminal)
                await response.write_eof()
                return response
            except asyncio.CancelledError:
                stalled_for.append(time.monotonic() - started)
                raise

        async with loopback_engine(tmp_path, service, respond) as (adapter, _engine_client):
            async with gateway_client(service, backend, models[backend]) as (_gateway, client, url, headers):

                async def exchange() -> tuple[int, bytes]:
                    async with client.post(
                        url, headers=headers, json={"model": "shared-model", "stream": True},
                    ) as response:
                        if stall == "backpressure":
                            # The engine keeps sending while the caller reads nothing.
                            await asyncio.sleep(bound * 3)
                        return response.status, await response.read()

                # Without the idle bound the stalled engine request never ends.
                status, body = await asyncio.wait_for(exchange(), timeout=5)
                assert status == 200
            assert adapter._active_transports == 0

        if stall in {"heartbeat", "unbounded", "backpressure"}:
            keepalives = b": keep-alive\n\n" * 4 if stall == "heartbeat" else b""
            assert body == metadata + keepalives + output + terminal
            assert len(calls) == 1 and stalled_for == []
            return
        # The engine request itself was cancelled, once, at the bound.
        assert len(stalled_for) == 1 and bound * 0.9 <= stalled_for[0] < bound + 2
        if stall == "after_output":
            # Output already reached the caller, so the stream ends in-protocol instead of replaying.
            assert len(calls) == 1
            assert body.startswith(metadata + output)
            ending = ProtocolSSEState(protocol)
            ending.observe(body[len(metadata + output):])
            assert ending.terminal_outcome == "failed_terminal"
        else:
            assert len(calls) == 2
            assert body == metadata + output + terminal
            assert clock.delays == [1]

    asyncio.run(run())


@pytest.mark.parametrize(
    "origin,vendor,source_protocol,bound",
    [
        # An unmeasured Source keeps Vibey's caller-owned bound.
        ("vibey", "xai", "openai_responses", "caller"),
        # A measured Anthropic stream keeps its shorter inter-byte bound after
        # the first response byte; Vibey's larger bound applies only before it.
        ("vibey", "anthropic", "anthropic", "idle"),
        # A native caller keeps its own bound, and an unmeasured Source stays unbounded here.
        ("codex", "xai", "openai_responses", None),
    ],
)
def test_vibey_hands_its_silence_bound_to_the_engine_read(
    monkeypatch, tmp_path, origin, vendor, source_protocol, bound,
):
    """MH-RUNTIME-013: before model output only the engine read sees the upstream, so it holds Vibey's bound.

    The gateway withholds the response, heartbeats included, until the first
    model output. Vibey therefore gives the engine its 600-second caller-owned
    pre-response patience; a measured Source resumes its shorter 120-second
    inter-byte bound after the first response byte.
    """
    idle, caller = 0.3, 0.8
    monkeypatch.setattr(engine_client_module, "_STREAM_IDLE_TIMEOUT_SECONDS", idle)
    monkeypatch.setattr(engine_adapter_module, "PROVIDER_SILENCE_TIMEOUT_S", caller)

    async def run() -> None:
        source = _source("src_silent01", "Silent", vendor=vendor, protocol=source_protocol)
        service, _clock, _models = configured_service(tmp_path, sources=[source])
        metadata, output, terminal, _buffered = WIRE[source_protocol]
        stalled_for: list[float] = []

        async def respond(request: web.Request) -> web.StreamResponse:
            await request.json()
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(metadata)
            started = time.monotonic()
            try:
                await asyncio.sleep(caller * 2)
            except asyncio.CancelledError:
                stalled_for.append(time.monotonic() - started)
                raise
            await response.write(output)
            await response.write(terminal)
            await response.write_eof()
            return response

        async with loopback_engine(tmp_path, service, respond) as (adapter, _engine_client):
            handle = await asyncio.wait_for(
                adapter.invoke(source.id, "shared-model", {"stream": True}, True, origin), timeout=5,
            )
            body = b"".join([chunk async for chunk in handle.stream]) if handle.stream is not None else b""
            outcome = await asyncio.wait_for(handle.outcome(), timeout=5)
            await asyncio.sleep(0.05)

        if bound is None:
            assert outcome.kind == RawOutcomeKind.SUCCESS
            assert body == metadata + output + terminal
            assert stalled_for == []
            return
        assert outcome.kind == RawOutcomeKind.TIMEOUT
        assert body == b""
        expected = caller if bound == "caller" else idle
        assert len(stalled_for) == 1 and expected * 0.9 <= stalled_for[0] < min(expected + 0.4, caller * 2)

    asyncio.run(run())


@pytest.mark.parametrize("vendor,protocol", [("anthropic", "anthropic"), ("openai", "openai_responses")])
@pytest.mark.parametrize("origin", ["vibey", "native"])
@pytest.mark.parametrize("phase", ["headers", "first_byte", "split_prelude"])
def test_measured_stream_prelude_uses_one_caller_budget_until_first_byte(
    monkeypatch, tmp_path, vendor, protocol, origin, phase,
):
    """MH-RUNTIME-013: slow relay preludes use caller patience, not a fresh budget per phase.

    Vibey can wait beyond measured idle for headers or the first body byte;
    native callers cannot. Splitting the delay across headers and body must
    not reset the budget. The existing idle test covers later byte gaps.
    """

    idle, caller = 0.3, 0.8
    delay = (idle + caller) / 2
    monkeypatch.setattr(engine_client_module, "_STREAM_IDLE_TIMEOUT_SECONDS", idle)
    monkeypatch.setattr(engine_adapter_module, "PROVIDER_SILENCE_TIMEOUT_S", caller)

    async def run() -> None:
        source = _source("src_slowheaders", "Slow prelude", vendor=vendor, protocol=protocol)
        service, _clock, _models = configured_service(tmp_path, sources=[source])
        metadata, output, terminal, _buffered = WIRE[protocol]

        async def respond(request: web.Request) -> web.StreamResponse:
            await request.json()
            if phase in {"headers", "split_prelude"}:
                await asyncio.sleep(delay)
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            if phase in {"first_byte", "split_prelude"}:
                await asyncio.sleep(delay)
            await response.write(metadata + output + terminal)
            await response.write_eof()
            return response

        caller_origin = origin if origin == "vibey" else "claude" if protocol == "anthropic" else "codex"
        async with loopback_engine(tmp_path, service, respond) as (adapter, _engine_client):
            handle = await asyncio.wait_for(
                adapter.invoke(source.id, "shared-model", {"stream": True}, True, caller_origin),
                timeout=3,
            )
            body = b"".join([chunk async for chunk in handle.stream]) if handle.stream is not None else b""
            outcome = await asyncio.wait_for(handle.outcome(), timeout=3)
            assert adapter._active_transports == 0

        if origin == "vibey" and phase != "split_prelude":
            assert outcome.kind == RawOutcomeKind.SUCCESS
            assert body == metadata + output + terminal
        else:
            assert outcome.kind == RawOutcomeKind.TIMEOUT
            assert body == b""

    asyncio.run(run())
