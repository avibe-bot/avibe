"""Request-private 503 recovery must not make an unlisted model a Source outage."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from config.v2_config import ModelHubModelConfig, ModelHubSourceStateConfig

from core.handlers.model_hub.adapter import RawOutcomeKind
from core.handlers.model_hub.provenance import render_turn_outcome_copy
from core.handlers.model_hub.retry import RECOVERY_EXHAUSTED_CODE, RecoveryRequest, source_identity
from core.handlers.model_hub.service import ModelHubError
from tests.test_model_hub_l3 import LiveInvokeHandle, _outcome, _source
from tests.test_model_hub_retry_policy import clock_service, fail


MODEL = "gpt-6-astra"
SOURCE = "src_recovery01"


def unlisted_service(tmp_path, outcomes=None, sources=None):
    service, clock = clock_service(tmp_path, sources=sources, outcomes=outcomes)
    service.store.config.agents["codex"].routes = {}
    return service, clock


def refusal(source_id=SOURCE, retry_after=None):
    return replace(
        _outcome(RawOutcomeKind.HTTP_ERROR, source_id=source_id, status=503),
        upstream_detail="No available accounts", retry_after=retry_after,
    )


@pytest.mark.parametrize("listed", [False, True])
def test_fast_503_waits_but_only_listed_model_changes_shared_health(tmp_path, listed):
    """MH-RETRY-UNLISTED-001: an unlisted 503 waits privately without cooling its Source."""
    async def run():
        service, clock = unlisted_service(tmp_path, outcomes=[refusal() for _ in range(4)])
        snapshots = []
        with pytest.raises(ModelHubError) as raised:
            await service.resolve_with_recovery(
                backend="codex", model_id="shared-model" if listed else MODEL, request={},
                recovery_observer=snapshots.append,
            )
        assert raised.value.code == RECOVERY_EXHAUSTED_CODE
        assert clock.delays == [30, 60]
        assert clock.elapsed == 90
        assert len(service.adapter.invocations) == 3
        assert snapshots[-1] is None
        assert service.store.config.sources[0].state.status == ("cooldown" if listed else "standby")
        assert bool(service.recovery.annotations(service.store.load())) is listed
        if not listed:
            assert render_turn_outcome_copy(raised.value.turn_outcome, "en") == (
                f"The provider doesn't list {MODEL}; try another model."
            )
    asyncio.run(run())


def test_private_wait_does_not_block_listed_model_on_same_source(tmp_path):
    """MH-RETRY-UNLISTED-001: private waiting does not block a listed peer on the same Source."""
    async def run():
        service, clock = unlisted_service(tmp_path, outcomes=[
            refusal(), _outcome(RawOutcomeKind.SUCCESS, source_id=SOURCE), refusal(), refusal(),
        ])
        asleep = asyncio.Event()
        release = asyncio.Event()

        async def sleep(seconds):
            asleep.set()
            await release.wait()
            clock.advance(seconds)

        service.recovery.sleep = sleep
        failed = asyncio.create_task(service.resolve_with_recovery(backend="codex", model_id=MODEL, request={}))
        try:
            await asyncio.wait_for(asleep.wait(), 2)
            assert service.store.config.sources[0].state.status == "standby"
            result = await service.resolve(backend="codex", model_id="shared-model", request={})
            assert result.outcome.kind is RawOutcomeKind.SUCCESS
        finally:
            failed.cancel()
            with pytest.raises(asyncio.CancelledError):
                await failed
        assert not service.recovery.annotations(service.store.load())
        assert not service._mutation_lock.locked()
    asyncio.run(run())


def test_private_wait_preserves_global_half_open_owner(tmp_path):
    """MH-RETRY-UNLISTED-001: a private deadline never replaces shared in-flight ownership."""
    async def run():
        service, clock = unlisted_service(tmp_path)
        source = service.store.load().sources[0]
        await fail(service, source, "server_error")
        clock.advance(30)
        source = service.store.load().sources[0]
        assert service.recovery.claim(source, service._reserve_settlement_generation(source.id))
        pending = RecoveryRequest(service.recovery)
        pending.defer_unlisted(source.id, source_identity(source), refusal(retry_after="300"))
        merged = pending.annotations(service.store.load(), service.recovery.annotations(service.store.load()))[source.id]
        assert merged.in_flight is True
        assert merged.retry_at is not None
    asyncio.run(run())


def test_private_wait_preserves_retired_cooldown_marker(tmp_path):
    """MH-RETRY-UNLISTED-001: private overlay retains the global retired-cooldown marker."""
    async def run():
        service, clock = unlisted_service(tmp_path)
        source = service.store.load().sources[0]
        await fail(service, source, "server_error")
        clock.advance(30)
        assert service.recovery.succeeded(source)
        shared = service.recovery.annotations(service.store.load())[source.id]
        pending = RecoveryRequest(service.recovery)
        pending.defer_unlisted(source.id, source_identity(source), refusal(retry_after="300"))
        merged = pending.annotations(service.store.load(), {source.id: shared})[source.id]
        assert merged.reason == "server_error"
        assert merged.retired_cooldown == shared.retired_cooldown
        assert merged.retry_at is not None
    asyncio.run(run())


def test_private_retry_after_keeps_the_earlier_fallback_available(tmp_path):
    async def run():
        second = "src_backup001"
        service, clock = unlisted_service(tmp_path, sources=[
            _source(SOURCE, "Long advice"), _source(second, "Earlier retry"),
        ], outcomes=[refusal(retry_after="300"), refusal(second), _outcome(RawOutcomeKind.SUCCESS, source_id=second)])
        result = await service.resolve_with_recovery(backend="codex", model_id=MODEL, request={})
        assert result.source_id == second
        assert clock.delays == [30]
        assert [call[0] for call in service.adapter.invocations] == [SOURCE, second, second]
        assert all(source.state.status == "standby" for source in service.store.config.sources)
        assert not service.recovery.annotations(service.store.load())
    asyncio.run(run())


@pytest.mark.parametrize("initially_listed", [False, True])
def test_inventory_membership_is_frozen_at_admission(tmp_path, initially_listed):
    async def run():
        service, _clock = unlisted_service(tmp_path, outcomes=[refusal()])
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
        with pytest.raises(ModelHubError):
            await service.resolve(backend="codex", model_id=MODEL, request={})
        assert service.store.config.sources[0].state.status == ("cooldown" if initially_listed else "standby")
    asyncio.run(run())


@pytest.mark.parametrize("status,expected", [(500, "cooldown"), (502, "cooldown"), (503, "standby"),
                                              (429, "cooldown"), (401, "needs_action"), (403, "needs_action")])
def test_only_unlisted_server_503_is_exempt(tmp_path, status, expected):
    async def run():
        service, _clock = unlisted_service(tmp_path, outcomes=[replace(refusal(), http_status=status)])
        with pytest.raises(ModelHubError):
            await service.resolve(backend="codex", model_id=MODEL, request={})
        assert service.store.config.sources[0].state.status == expected
    asyncio.run(run())


def test_configuration_wake_cannot_bypass_private_deadline(tmp_path):
    async def run():
        service, clock = unlisted_service(tmp_path, outcomes=[refusal(), _outcome(RawOutcomeKind.SUCCESS)])
        waits = []

        async def sleep(seconds):
            waits.append(seconds)
            if len(waits) == 1:
                service.recovery.notify()
            else:
                clock.advance(seconds)

        service.recovery.sleep = sleep
        await service.resolve_with_recovery(backend="codex", model_id=MODEL, request={})
        assert waits == [30, 30]
        assert clock.elapsed == 30
        assert len(service.adapter.invocations) == 2
    asyncio.run(run())


@pytest.mark.parametrize("change", ["identity", "auth_block"])
def test_configuration_change_rechecks_private_wait_against_global_authority(tmp_path, change):
    async def run():
        service, clock = unlisted_service(tmp_path, outcomes=[refusal(), _outcome(RawOutcomeKind.SUCCESS)])

        async def sleep(_seconds):
            source = service.store.config.sources[0]
            if change == "identity":
                source.credential_generation = "vp_newidentity"
            else:
                source.state = ModelHubSourceStateConfig(
                    status="needs_action", detail_key="models.source.needs_action.credential_revoked",
                )
            service.recovery.notify()

        service.recovery.sleep = sleep
        if change == "identity":
            await service.resolve_with_recovery(backend="codex", model_id=MODEL, request={})
            assert len(service.adapter.invocations) == 2
        else:
            with pytest.raises(ModelHubError):
                await service.resolve_with_recovery(backend="codex", model_id=MODEL, request={})
            assert len(service.adapter.invocations) == 1
            assert service.store.config.sources[0].state.status == "needs_action"
        assert clock.elapsed == 0
    asyncio.run(run())


def test_listed_failure_during_private_wait_keeps_its_global_deadline(tmp_path):
    async def run():
        service, clock = unlisted_service(tmp_path, outcomes=[refusal(), refusal(retry_after="90"), refusal()])
        waits = []

        async def sleep(seconds):
            waits.append(seconds)
            if len(waits) == 1:
                with pytest.raises(ModelHubError):
                    await service.resolve(backend="codex", model_id="shared-model", request={})
            clock.advance(seconds)

        service.recovery.sleep = sleep
        with pytest.raises(ModelHubError):
            await service.resolve_with_recovery(backend="codex", model_id=MODEL, request={})
        # The global failure wakes/cancels the first private timer before it advances.
        assert waits == [30, 90]
        assert clock.elapsed == 90
        assert len(service.adapter.invocations) == 3
        shared = service.recovery.annotations(service.store.load())[SOURCE]
        assert shared.reason == "server_error"
        assert service.store.config.sources[0].state.status == "cooldown"
    asyncio.run(run())


@pytest.mark.parametrize("ending", ["upstream_terminal", "downstream_cancel"])
@pytest.mark.parametrize("listed", [False, True])
def test_consumed_stream_failure_keeps_admitted_inventory_scope(tmp_path, ending, listed):
    """MH-RETRY-UNLISTED-001: stream settlement keeps the admission-time inventory fact."""
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
        assert len(service.adapter.invocations) == 1 and clock.delays == []
        assert service.store.config.sources[0].state.status == ("cooldown" if listed else "standby")
        assert not service._mutation_lock.locked()
    asyncio.run(run())
