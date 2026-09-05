from __future__ import annotations

import asyncio

import pytest

from config.v2_config import ModelHubBackendModelConfig, ModelHubModelConfig, ModelHubSourceStateConfig
from core.handlers.model_hub.adapter import RawCallOutcome, RawOutcomeKind
from core.handlers.model_hub.service import ModelHubError
from tests.test_model_hub_resolution import _service, _source
from tests.test_model_hub_routing_modes import MODEL, _sparse_config
from tests.test_model_hub_transport_leases import _transport


def _outcome(source, target=MODEL, *, status=200):
    return RawCallOutcome(
        kind=RawOutcomeKind.SUCCESS if status == 200 else RawOutcomeKind.HTTP_ERROR,
        http_status=status,
        error_code=None,
        redacted_message=None,
        stream_started=False,
        source_id=source.id,
        model_id=target,
    )


async def _save_route(service, hops):
    try:
        return await service.set_agent_chain("claude", MODEL, {"hops": hops})
    except ModelHubError as refusal:
        assert "would_interrupt" in refusal.data
        return await service.set_agent_chain("claude", MODEL, {"hops": hops, "force": True, **refusal.data})


def _gate_method(owner, name, *, call_number=1):
    entered, release = asyncio.Event(), asyncio.Event()
    original = getattr(owner, name)
    calls = 0

    async def gated(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == call_number:
            entered.set()
            await release.wait()
        return await original(*args, **kwargs)

    setattr(owner, name, gated)
    return entered, release


@pytest.mark.parametrize("probe", [False, True])
@pytest.mark.parametrize("change", ["target", "source", "empty"])
def test_demand_snapshot_is_revalidated_before_transport_and_observer(tmp_path, probe, change):
    async def run():
        source = _source("src_race0001", ())
        service, store, adapter = _service(tmp_path, _sparse_config(source))
        entered, release = _gate_method(service, "_prepare_engine_for_demand")
        observations = []
        task = asyncio.create_task(
            service.probe_agent("claude", MODEL)
            if probe
            else service.resolve(
                backend="claude", model_id=MODEL, request={}, attempt_observer=lambda *args: observations.append(args)
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        if change == "target":
            await _save_route(service, [{"source_id": source.id, "model_id": "new-target"}])
            expected = "new-target"
        elif change == "source":
            await service.patch_source(source.id, {"display_name": "Changed source"})
            expected = MODEL
        else:
            await _save_route(service, [])
            expected = None
        adapter.outcomes.append(_outcome(source, expected or MODEL))
        release.set()
        if expected is None:
            with pytest.raises(ModelHubError):
                await asyncio.wait_for(task, 2)
            assert adapter.invocations == []
            assert observations == []
        else:
            result = await asyncio.wait_for(task, 2)
            assert adapter.invocations == [(source.id, expected)]
            if not probe:
                assert result.model_id == expected
                assert result.source_label == store.config.sources[0].display_name
                assert [(row[0], row[1]) for row in observations] == [(source.id, expected)] * 2
            assert adapter.synced[-1][0].route_model_ids == (expected,)
        assert not service._mutation_lock.locked()

    asyncio.run(run())


@pytest.mark.parametrize("probe", [False, True])
def test_refresh_retry_never_uses_a_removed_exact_target(tmp_path, probe):
    async def run():
        source = _source("src_refresh01", ())
        service, _, adapter = _service(tmp_path, _sparse_config(source))
        adapter.refreshable_credential_refs.add(source.credential_ref)
        adapter.outcomes.extend([_outcome(source, status=401), _outcome(source, "new-target")])
        entered, release = _gate_method(service, "_invoke_admitted", call_number=2)
        observations = []
        task = asyncio.create_task(
            service.probe_agent("claude", MODEL)
            if probe
            else service.resolve(
                backend="claude", model_id=MODEL, request={}, attempt_observer=lambda *args: observations.append(args)
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        await _save_route(service, [{"source_id": source.id, "model_id": "new-target"}])
        release.set()
        result = await asyncio.wait_for(task, 2)
        assert adapter.invocations == [(source.id, MODEL), (source.id, "new-target")]
        assert (result["model_id"] if probe else result.model_id) == "new-target"
        if not probe:
            begun = [row for row in observations if row[4] is None]
            assert [(row[0], row[1]) for row in begun] == adapter.invocations

    asyncio.run(run())


def test_fallback_replans_unattempted_hops_but_keeps_failed_source_excluded(tmp_path):
    async def run():
        first, second = _source("src_failed001", ()), _source("src_second001", ())
        service, store, adapter = _service(tmp_path, _sparse_config(first, second))
        adapter.outcomes.extend([_outcome(first, status=503), _outcome(second, "new-target")])
        entered, release = _gate_method(service, "_invoke_admitted", call_number=2)
        task = asyncio.create_task(service.resolve(backend="claude", model_id=MODEL, request={}))
        await asyncio.wait_for(entered.wait(), 2)
        # A recovered first Source must still not be retried in this turn.
        async with service._mutation_lock:
            previous = store.load()
            updated = service._clone_config(previous)
            updated.sources[0].state = ModelHubSourceStateConfig(status="standby")
            await service._commit_synced(previous, updated)
        await _save_route(
            service,
            [
                {"source_id": first.id, "model_id": "retry-first"},
                {"source_id": second.id, "model_id": "new-target"},
            ],
        )
        release.set()
        result = await asyncio.wait_for(task, 2)
        assert result.source_id == second.id
        assert adapter.invocations == [(first.id, MODEL), (second.id, "new-target")]

    asyncio.run(run())


def test_same_target_source_configuration_change_rebuilds_exact_request(tmp_path):
    async def run():
        source = _source("src_capability", ())
        service, store, adapter = _service(tmp_path, _sparse_config(source))
        entered, release = _gate_method(service, "_prepare_engine_for_demand")
        adapter.outcomes.append(_outcome(source))
        observations = []
        task = asyncio.create_task(
            service.resolve(
                backend="claude",
                model_id=MODEL,
                request={"reasoning_effort": "high"},
                attempt_observer=lambda *args: observations.append(args),
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        async with service._mutation_lock:
            previous = store.load()
            updated = service._clone_config(previous)
            updated.sources[0].credential_ref = "cred_replacement"
            updated.sources[0].models = [
                ModelHubModelConfig(
                    id=MODEL,
                    provenance="discovered",
                    reasoning_efforts=["high"],
                )
            ]
            await service._commit_synced(previous, updated)
        release.set()
        await asyncio.wait_for(task, 2)
        assert adapter.invocations == [(source.id, MODEL)]
        assert adapter.invocation_requests == [{"reasoning_effort": "high"}]
        assert adapter.synced[-1][0].credential_ref == "cred_replacement"
        assert observations[0][6] == ()

    asyncio.run(run())


async def _transport_service(tmp_path, adapter, binding):
    source = _source(binding.source_id, (), vendor="custom", credential_ref=binding.credential_ref)
    source.protocol, source.base_url = binding.protocol, binding.base_url
    config = _sparse_config(source)
    config.agents["claude"].mode = "direct"
    agent = config.agents["opencode"]
    agent.mode = "hub"
    agent.models = [ModelHubBackendModelConfig(id="unknown", native_protocol="openai_chat")]
    agent.sources.order = [source.id]
    service, _, _ = _service(tmp_path / "service", config, adapter)
    adapter.state_store.sync_sources(service._bindings(config))
    service._engine_synced = True

    async def prepare(**_kwargs):
        await service._ensure_engine_synced()

    service._prepare_engine_for_demand = prepare
    return service


@pytest.mark.parametrize("stream", [False, True])
def test_independent_http_progress_after_admission_releases_mutation_lock(tmp_path, stream):
    async def run():
        async with _transport(tmp_path) as (adapter, _supervisor, binding, received, finish, requests):
            service = await _transport_service(tmp_path, adapter, binding)
            first = asyncio.create_task(
                service.resolve(backend="opencode", model_id="unknown", request={}, stream=stream)
            )
            await asyncio.wait_for(received.wait(), 2)
            assert not service._mutation_lock.locked()
            second = asyncio.create_task(service.resolve(backend="opencode", model_id="unknown", request={}))
            async with asyncio.timeout(2):
                while len(requests) < 2:
                    await asyncio.sleep(0)
            assert adapter._active_transports == 2
            assert not service._mutation_lock.locked()
            finish.set()
            results = await asyncio.wait_for(asyncio.gather(first, second), 2)
            for result in results:
                if result.handle.stream is not None:
                    async for _part in result.handle.stream:
                        pass
                await result.handle.close_stream()
            assert adapter._active_transports == 0

    asyncio.run(run())


@pytest.mark.parametrize("probe", [False, True])
def test_cancel_before_adapter_admission_releases_service_exclusion_without_attempt(tmp_path, probe):
    async def run():
        async with _transport(tmp_path) as (adapter, _supervisor, binding, _received, _finish, requests):
            service = await _transport_service(tmp_path, adapter, binding)
            observations = []
            entered = asyncio.Event()
            invoke = adapter.invoke

            async def entering(*args, **kwargs):
                entered.set()
                return await invoke(*args, **kwargs)

            adapter.invoke = entering
            await adapter._routing_lock.acquire()
            task = asyncio.create_task(
                service.probe_agent("opencode", "unknown")
                if probe
                else service.resolve(
                    backend="opencode",
                    model_id="unknown",
                    request={},
                    attempt_observer=lambda *args: observations.append(args),
                )
            )
            await asyncio.wait_for(entered.wait(), 2)
            assert service._mutation_lock.locked()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            assert not service._mutation_lock.locked()
            adapter._routing_lock.release()
            assert requests == observations == []
            assert adapter._active_transports == 0

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["callback", "origin", "missing_source"])
def test_adapter_pre_network_failures_do_not_leak_leases_or_service_exclusion(tmp_path, failure):
    async def run():
        async with _transport(tmp_path) as (adapter, _supervisor, binding, _received, _finish, requests):
            service = await _transport_service(tmp_path, adapter, binding)
            if failure != "callback":
                from dataclasses import replace

                adapter.state_store.sync_sources(
                    [] if failure == "missing_source" else [replace(binding, allowed_origins=("claude",))]
                )

            def observer(*_args):
                raise ValueError("fixture callback failure")

            with pytest.raises(ModelHubError):
                await service.resolve(
                    backend="opencode",
                    model_id="unknown",
                    request={},
                    attempt_observer=observer,
                )
            assert not service._mutation_lock.locked()
            assert not adapter._routing_lock.locked()
            assert adapter._active_transports == 0
            assert requests == []

    asyncio.run(run())
