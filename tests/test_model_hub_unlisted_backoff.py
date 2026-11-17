"""Unlisted manual targets fail once without retrying or poisoning shared supply."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import replace

import pytest

from config.v2_config import ModelHubBackendModelConfig, ModelHubConfig, ModelHubModelConfig, ModelHubRouteConfig, ModelHubRouteHopConfig
from core.handlers.model_hub.adapter import RawOutcomeKind
from core.handlers.model_hub.retry import RECOVERY_EXHAUSTED_CODE
from core.handlers.model_hub.service import ModelHubError
from tests.test_model_hub_l3 import LiveInvokeHandle, _outcome, _source
from tests.test_model_hub_retry_policy import clock_service, fail


MODEL = "gpt-6-astra"
SOURCE = "src_recovery01"


def unlisted_service(tmp_path, outcomes=None, sources=None, *, backend="codex", target=MODEL, menu=MODEL):
    service, clock = clock_service(tmp_path, sources=sources, outcomes=outcomes)
    if backend != "codex":
        service.store.config.agents[backend] = copy.deepcopy(service.store.config.agents["codex"])
    agent = service.store.config.agents[backend]
    agent.backend = backend
    if not any(model.id == menu for model in agent.models):
        agent.models.append(ModelHubBackendModelConfig(id=menu, origin="manual"))
    agent.routes = {
        menu: ModelHubRouteConfig(hops=tuple(
            ModelHubRouteHopConfig(source.id, target) for source in service.store.config.sources
        )),
    }
    ModelHubConfig.from_payload(service.store.config.to_payload())
    return service, clock


def refusal(source_id=SOURCE, *, status=503, code=None):
    return replace(
        _outcome(RawOutcomeKind.HTTP_ERROR, source_id=source_id, status=status, code=code),
        upstream_detail="No available accounts", retry_after="300",
    )


@pytest.mark.parametrize("backend", ["codex", "vibey"])
@pytest.mark.parametrize("status", [500, 502, 503, 504, 529, 599])
@pytest.mark.parametrize("code", [None, "api_error", "overloaded_error"])
def test_unlisted_server_refusal_is_request_local_and_prompt(tmp_path, backend, status, code):
    """MH-RETRY-UNLISTED-001: manual unknown-target 5xx fails once without waiting or shared health changes."""
    async def run():
        service, clock = unlisted_service(tmp_path, outcomes=[refusal(status=status, code=code)], backend=backend)
        before = copy.deepcopy(service.store.config.sources[0].to_payload())
        snapshots = []
        with pytest.raises(ModelHubError) as raised:
            await service.resolve_with_recovery(
                backend=backend, model_id=MODEL, request={}, recovery_observer=snapshots.append,
            )
        assert raised.value.code == "upstream_request_invalid"
        assert raised.value.status == 400
        assert raised.value.turn_outcome.discriminator == "request_nonfallback"
        assert raised.value.turn_outcome.supply_facts is None
        assert service.adapter.invocations == [(SOURCE, MODEL, backend)]
        assert clock.delays == [] and clock.elapsed == 0
        assert all(snapshot is None for snapshot in snapshots)
        assert service.store.config.sources[0].to_payload() == before
        assert service.recovery.annotations(service.store.load()) == {}
        assert service.events.list(limit=10) == []
        assert not service._mutation_lock.locked()
    asyncio.run(run())


def test_anthropic_relay_mapped_unknown_target_does_not_block_listed_peer(tmp_path):
    """MH-RETRY-UNLISTED-001: the Incus relay case leaves a listed Claude peer usable immediately."""
    async def run():
        menu, target = "claude-fable-5-1", "not-a-model-xyz"
        source = _source(SOURCE, "Relay", protocol="anthropic", model_id=menu)
        service, clock = unlisted_service(
            tmp_path, sources=[source], outcomes=[refusal(code="api_error"), _outcome(RawOutcomeKind.SUCCESS)],
            backend="vibey", target=target, menu=menu,
        )
        source_before = copy.deepcopy(source.to_payload())
        with pytest.raises(ModelHubError) as raised:
            await service.resolve_with_recovery(backend="vibey", model_id=menu, request={})
        assert raised.value.code == "upstream_request_invalid" and raised.value.status == 400
        assert service.store.config.sources[0].to_payload() == source_before
        # A separate catalog row maps to the Source's listed peer without changing
        # the failing manual override or pretending its target was discovered.
        peer = "listed-peer"
        agent = service.store.config.agents["vibey"]
        agent.models.append(ModelHubBackendModelConfig(id=peer, origin="manual"))
        agent.routes[peer] = ModelHubRouteConfig(hops=(ModelHubRouteHopConfig(SOURCE, menu),))
        result = await service.resolve_with_recovery(backend="vibey", model_id=peer, request={})
        assert result.outcome.kind is RawOutcomeKind.SUCCESS
        assert service.adapter.invocations == [(SOURCE, target, "vibey"), (SOURCE, menu, "vibey")]
        assert clock.delays == [] and service.recovery.annotations(service.store.load()) == {}
    asyncio.run(run())


def test_unlisted_refusal_does_not_fallback_to_another_manual_hop(tmp_path):
    """MH-RETRY-UNLISTED-001: the refusal ends the request; a later manual hop is not tried."""
    async def run():
        second = "src_backup001"
        service, clock = unlisted_service(tmp_path, sources=[_source(SOURCE, "First"), _source(second, "Second")],
                                          outcomes=[refusal(), _outcome(RawOutcomeKind.SUCCESS, source_id=second)])
        with pytest.raises(ModelHubError) as raised:
            await service.resolve_with_recovery(backend="codex", model_id=MODEL, request={})
        assert raised.value.code == "upstream_request_invalid"
        assert service.adapter.invocations == [(SOURCE, MODEL, "codex")]
        assert all(source.state.status == "standby" for source in service.store.config.sources)
        assert clock.delays == [] and service.events.list(limit=10) == []
    asyncio.run(run())


def test_listed_server_error_still_retries_and_changes_shared_health(tmp_path):
    async def run():
        service, clock = unlisted_service(tmp_path, outcomes=[replace(refusal(), retry_after=None) for _ in range(4)])
        with pytest.raises(ModelHubError) as raised:
            await service.resolve_with_recovery(backend="codex", model_id="shared-model", request={})
        assert raised.value.code == RECOVERY_EXHAUSTED_CODE
        assert clock.delays == [30, 60]
        assert len(service.adapter.invocations) == 3
        assert service.store.config.sources[0].state.status == "cooldown"
        assert service.recovery.annotations(service.store.load())
    asyncio.run(run())


@pytest.mark.parametrize("initially_listed", [False, True])
def test_inventory_membership_is_frozen_at_admission(tmp_path, initially_listed):
    """MH-RETRY-UNLISTED-001: membership at admission decides, not inventory changed mid-call."""
    async def run():
        service, clock = unlisted_service(tmp_path, outcomes=[refusal()])
        source = service.store.config.sources[0]
        if initially_listed:
            source.models.append(ModelHubModelConfig(id=MODEL, provenance="discovered"))
        invoke = service.adapter.invoke

        async def change_inventory(*args, **kwargs):
            handle = await invoke(*args, **kwargs)
            if initially_listed:
                source.models = [model for model in source.models if model.id != MODEL]
            else:
                source.models.append(ModelHubModelConfig(id=MODEL, provenance="discovered"))
            return handle

        service.adapter.invoke = change_inventory
        with pytest.raises(ModelHubError) as raised:
            await service.resolve(backend="codex", model_id=MODEL, request={})
        assert service.adapter.invocations == [(SOURCE, MODEL, "codex")]
        assert service.store.config.sources[0].state.status == ("cooldown" if initially_listed else "standby")
        assert (raised.value.code == "upstream_request_invalid") is not initially_listed
        assert clock.delays == []
    asyncio.run(run())


@pytest.mark.parametrize("status,code,expected", [
    (429, None, "cooldown"), (401, None, "needs_action"), (403, None, "needs_action"),
    (503, "rate_limit_error", "cooldown"), (503, "account_banned", "needs_action"),
])
def test_unlisted_target_preserves_named_credential_and_limit_policies(tmp_path, status, code, expected):
    async def run():
        service, _ = unlisted_service(tmp_path, outcomes=[refusal(status=status, code=code)])
        with pytest.raises(ModelHubError):
            await service.resolve(backend="codex", model_id=MODEL, request={})
        assert service.adapter.invocations == [(SOURCE, MODEL, "codex")]
        assert service.store.config.sources[0].state.status == expected
    asyncio.run(run())


@pytest.mark.parametrize("ending", ["upstream_terminal", "downstream_cancel"])
@pytest.mark.parametrize("listed", [False, True])
def test_consumed_stream_failure_keeps_admitted_inventory_scope(tmp_path, ending, listed):
    """MH-RETRY-UNLISTED-001: stream settlement uses frozen membership, not mutated inventory."""
    async def run():
        service, clock = unlisted_service(tmp_path)
        source = service.store.config.sources[0]
        if listed:
            source.models.append(ModelHubModelConfig(id=MODEL, provenance="discovered"))
        handle = LiveInvokeHandle(replace(refusal(), stream_started=True), (b"output",))
        service.adapter.live_handles.append(handle)
        resolved = await service.resolve_with_recovery(backend="codex", model_id=MODEL, request={}, stream=True)
        source.models = [] if listed else [ModelHubModelConfig(id=MODEL, provenance="discovered")]
        async for _ in handle.stream:
            pass
        settlement = await service.settle_handle_outcome(
            resolved, await handle.outcome(), termination_origin=ending, record_attempt=lambda *_: None,
        )
        assert settlement.decision.action == "surface"
        assert settlement.decision.reason == ("server_error" if listed else None)
        assert len(service.adapter.invocations) == 1 and clock.delays == []
        assert service.store.config.sources[0].state.status == ("cooldown" if listed else "standby")
        assert not service._mutation_lock.locked()
    asyncio.run(run())


def test_manual_unlisted_probe_cannot_cool_source(tmp_path):
    """MH-RETRY-UNLISTED-001: a probe of an unlisted target refuses without cooling its Source."""
    service, clock = unlisted_service(tmp_path, outcomes=[refusal(status=529, code="overloaded_error")])
    result = asyncio.run(service.probe_agent("codex", MODEL))
    assert result["reachable"] is False
    assert service.adapter.invocations == [(SOURCE, MODEL, "codex")]
    assert service.store.config.sources[0].state.status == "standby"
    assert clock.delays == [] and service.recovery.annotations(service.store.load()) == {}
    assert service.events.list(limit=10) == []


def test_unlisted_refusal_releases_half_open_owner_without_replacing_prior_failure(tmp_path):
    """MH-RETRY-UNLISTED-001: the refusal frees a half-open slot and leaves the prior Source failure as it was."""
    async def run():
        service, clock = unlisted_service(tmp_path, outcomes=[refusal()])
        await fail(service, service.store.load().sources[0], "server_error")
        clock.advance(30)
        before = copy.deepcopy(service.store.load().sources[0].to_payload())
        events_before = service.events.list(limit=10)
        with pytest.raises(ModelHubError) as raised:
            await service.resolve_with_recovery(backend="codex", model_id=MODEL, request={})
        assert raised.value.code == "upstream_request_invalid"
        assert service.adapter.invocations == [(SOURCE, MODEL, "codex")]
        assert service.store.load().sources[0].to_payload() == before
        assert service.events.list(limit=10) == events_before
        assert not service.recovery.annotations(service.store.load())[SOURCE].in_flight
        assert clock.delays == []
    asyncio.run(run())
