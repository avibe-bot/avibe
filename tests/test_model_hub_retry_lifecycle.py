"""Recovery ownership and persistence boundaries from the first exact-head review."""

from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import aiohttp
from aiohttp import web
import pytest

from config.v2_config import ModelHubConfig, ModelHubRouteConfig, ModelHubRouteHopConfig, ModelHubSourceStateConfig
from core.handlers.model_hub.adapter import RawOutcomeKind
from core.handlers.model_hub.retry import RECOVERY_EXHAUSTED_CODE, RECOVERY_EXHAUSTED_MESSAGE
from core.handlers.model_hub.service import ModelHubError
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from core.run_settlement import SETTLED_BY_TERMINAL_RESULT
from modules.agents.model_hub import ModelHubRuntimeRouter, bind_launch
from tests.test_model_hub_l3 import _assert_valid, _canonicalize_fixed_test_routes, _outcome, _source
from tests.test_model_hub_retry_policy import WIRE, clock_service, fail
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
async def loopback_engine(tmp_path, service, respond):
    """Use the production client/admission/stream observer against local HTTP."""
    config = service.store.load()
    source = config.sources[0]
    state = EngineStateStore(tmp_path / "engine")
    source.credential_ref = state.store_api_key("fixture", vendor=source.vendor, protocol=source.protocol)
    service.store.save(config)
    state.sync_sources(service._bindings(config))
    app = web.Application()
    app.router.add_post("/{path:.*}", respond)
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
            current.verification_pending = "vp_" + "1" * 32
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
        async with gateway_client(service, backend, models[backend]) as (gateway, client, url, headers):
            async with client.post(url, headers=headers, json={"model": "shared-model"}) as response:
                assert response.status == (400 if backend == "codex" else 424)
                assert (await response.json())["error"]["code"] == RECOVERY_EXHAUSTED_CODE
            projection = gateway.correlation.terminal_projection("turn_lifecycle", backend=backend)
            if backend == "opencode":
                assert projection is None  # Shared-server HTTP does not prove an exact Turn.
            else:
                assert projection is not None
                assert [blocker.reason for blocker in projection.supply_facts.blockers] == [
                    code or ("server_error" if status == 503 else "rate_limited"),
                ]
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
                        "message": RECOVERY_EXHAUSTED_MESSAGE,
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
