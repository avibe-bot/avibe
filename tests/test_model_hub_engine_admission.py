"""Engine admission contracts missing from inference-only transport tests.

Regression risks: holding routing during drain deadlocks OAuth continuations;
counting only transports swaps during credential work; restarting a withdrawal
budget or classifying its terminal result as network fallback replays upstream.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core.handlers.model_hub.adapter import DiscoveredModel, RawCallOutcome, RawOutcomeKind
from core.handlers.model_hub.classification import classify_outcome
from vibe.model_hub_runtime.admission import EngineAdmission, engine_operation
from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter
from vibe.model_hub_runtime.client import completed_handle
from vibe.model_hub_runtime.supervisor import EngineUnavailableError


def adapter():
    return CLIProxyEngineAdapter(supervisor=SimpleNamespace(state_store=object()))


@pytest.mark.asyncio
async def test_routine_drain_releases_routing_for_admitted_continuations():
    owner = adapter()
    flow = owner._engine_admission.retain()
    drain = asyncio.create_task(owner._drain_engine())
    await asyncio.sleep(0)
    assert not owner._routing_lock.locked()
    with pytest.raises(EngineUnavailableError):
        owner._engine_admission.acquire()
    # A continuation can take routing and finish the SAME flow while the gate
    # excludes fresh calls; no lock needed by it is held by the drain.
    lease, owns = owner._engine_admission.acquire(flow)
    assert lease is flow and not owns
    async with owner._routing_lock:
        owner._engine_admission.release(flow)
    assert await drain is True


@pytest.mark.asyncio
async def test_routine_timeout_defers_without_cancelling_or_releasing_work(monkeypatch):
    from vibe.model_hub_runtime import adapter as module

    monkeypatch.setattr(module, "_ENGINE_ROUTINE_DRAIN_SECONDS", 0.01)
    owner = adapter()
    work = owner._engine_admission.retain()
    interrupted = []
    work.abort = lambda: interrupted.append(True)
    assert await owner._drain_engine() is False
    assert work.active and interrupted == []
    assert owner._engine_admission.reason is None
    owner._engine_admission.release(work)


@pytest.mark.asyncio
async def test_withdrawal_budget_does_not_restart_and_signals_each_work_unit_once():
    owner = adapter()
    work = owner._engine_admission.retain()
    interrupted = []
    work.abort = lambda: interrupted.append("withdrawn")
    deadline = asyncio.get_running_loop().time() - 0.01
    owner._withdrawal_deadline = deadline
    assert await owner._drain_engine(withdrawal=True) is False
    assert owner._withdrawal_deadline == deadline
    assert await owner._drain_engine(withdrawal=True) is False
    assert interrupted == ["withdrawn"]
    with pytest.raises(EngineUnavailableError) as denied:
        owner._engine_admission.acquire(work)
    assert denied.value.reason == "engine_withdrawn"
    assert not owner._routing_lock.locked()
    owner._engine_admission.release(work)


@pytest.mark.asyncio
async def test_cancelled_routine_selection_reopens_healthy_admission():
    owner = adapter()
    owner.lifecycle = SimpleNamespace(
        recovered=True, _ready=True, withdrawn_current=False,
        prepare_candidate=lambda _generation: None,
        activate=lambda _generation, *, reason: None,
    )
    work = owner._engine_admission.retain()
    task = asyncio.create_task(owner._select_staged(object()))
    try:
        while owner._engine_admission.reason != "engine_updating":
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert owner._engine_admission.reason is None
    finally:
        owner._engine_admission.release(work)


@pytest.mark.asyncio
async def test_routine_drain_upgrades_to_learned_withdrawal_budget(monkeypatch):
    from vibe.model_hub_runtime import adapter as module

    monkeypatch.setattr(module, "_ENGINE_ROUTINE_DRAIN_SECONDS", 0.01)
    monkeypatch.setattr(module, "_ENGINE_WITHDRAWAL_DRAIN_SECONDS", 0.2)
    owner = adapter()
    owner.lifecycle = SimpleNamespace(recovered=True, _ready=True, withdrawn_current=False)
    work = owner._engine_admission.retain()
    drain = asyncio.create_task(owner._drain_engine())
    try:
        while owner._engine_admission.reason != "engine_updating":
            await asyncio.sleep(0)
        owner.lifecycle.withdrawn_current = True
        owner._gate_known_withdrawal()
        await asyncio.sleep(0.03)
        assert not drain.done()
        assert owner._engine_admission.reason == "engine_withdrawn"
    finally:
        owner._engine_admission.release(work)
    assert await drain is True


    started = asyncio.Event()
    finish = asyncio.Event()

    class Owner:
        lifecycle = None
        _engine_admission = EngineAdmission()
        release = "model-hub-engine-v8.0.63-2"

        def engine_release(self):
            return self.release

        @engine_operation
        async def discover_models(self):
            started.set()
            await finish.wait()
            return [DiscoveredModel("fixture-model")]

    owner = Owner()
    operation = asyncio.create_task(owner.discover_models())
    await started.wait()
    assert not owner._engine_admission.idle.is_set()
    owner._engine_admission.close("engine_updating")
    finish.set()
    result = await operation
    assert owner._engine_admission.idle.is_set()
    owner.release = "model-hub-engine-v8.0.63-3"
    assert result.engine_release == "model-hub-engine-v8.0.63-2"
    assert list(result) == [DiscoveredModel("fixture-model")]


@pytest.mark.asyncio
async def test_supplied_reconcile_decision_is_revalidated_under_update_lock():
    from core.model_hub_engine_channel import ResolutionAction

    owner = adapter()
    calls = []
    fresh = SimpleNamespace(action=ResolutionAction.BLOCKED, target=None)
    owner.lifecycle = SimpleNamespace(
        withdrawn_current=False,
        check=lambda **_kwargs: calls.append(True) or fresh,
        pause_withdrawn=lambda: None,
    )
    stale = SimpleNamespace(action=ResolutionAction.WITHDRAW, target=SimpleNamespace(asset_release_tag="stale"))
    await owner._reconcile_engine(explicit=False, decision=stale)
    assert calls == [True]


@pytest.mark.asyncio
async def test_successful_activation_rollback_reopens_routine_admission(tmp_path, monkeypatch):
    from tests.test_model_hub_engine_lifecycle import owner_fixture, signed_candidate

    owner, supervisor, _baseline = owner_fixture(tmp_path, monkeypatch)
    owner.ensure_running()
    candidate, _signer, _manifest, _envelope = signed_candidate(owner, tmp_path)
    supervisor.fail_tag = candidate.asset_release_tag
    adapter_owner = CLIProxyEngineAdapter(supervisor=supervisor, state_store=object())
    adapter_owner.lifecycle = owner
    try:
        with pytest.raises(EngineUnavailableError):
            await adapter_owner._select_staged(candidate)
        assert owner._ready
        assert adapter_owner._engine_admission.reason is None
    finally:
        adapter_owner._engine_admission.reopen()


@pytest.mark.asyncio
async def test_engine_free_search_and_fetch_bypass_cpa_admission():
    owner = adapter()
    owner._engine_admission.close("engine_withdrawn")
    search = await owner.search("source", "unknown", "service", None, {})
    fetch = await owner.fetch("source", "unknown", "service", None, {})
    assert search.error == "invalid_endpoint"
    assert fetch.error == "invalid_endpoint"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_started", [False, True])
async def test_withdrawal_outcome_is_explicit_terminal_and_never_fallback(stream_started):
    outcome = RawCallOutcome(
        kind=RawOutcomeKind.NETWORK_ERROR, http_status=None, error_code=None,
        redacted_message=None, stream_started=stream_started, model_id="model-a", source_id="src_fixture123",
    )
    handle = completed_handle(outcome)
    handle.engine_release = "model-hub-engine-v8.0.63-2"
    await handle.withdraw()
    await handle.withdraw()
    result = await handle.outcome()
    assert result.error_code == "engine_withdrawn"
    assert result.engine_release == "model-hub-engine-v8.0.63-2"
    decision = classify_outcome(result)
    assert decision.action == "surface" and decision.error_code == "engine_withdrawn"


@pytest.mark.asyncio
async def test_recall_gate_does_not_wait_for_routing_held_by_admitted_work():
    owner = adapter()
    work = owner._engine_admission.retain()
    owner._withdrawal_deadline = asyncio.get_running_loop().time() - 1
    async with owner._routing_lock:
        assert await asyncio.wait_for(owner._drain_engine(withdrawal=True), 0.1) is False
    assert work.interrupted
    owner._engine_admission.release(work)


@pytest.mark.asyncio
async def test_stuck_close_cannot_postpone_recall_terminal_or_release_lease():
    from vibe.model_hub_runtime.client import EngineInvokeHandle

    owner = adapter()
    work = owner._engine_admission.retain()
    close_allowed = asyncio.Event()

    async def close():
        try:
            await close_allowed.wait()
        finally:
            owner._engine_admission.release(work)

    handle = EngineInvokeHandle(
        stream=None, outcome=asyncio.get_running_loop().create_future(), stream_closer=close,
        model_id="model-a", source_id="src_fixture123",
    )
    handle.engine_release = "model-hub-engine-v8.0.63-2"
    work.abort = handle.withdraw
    owner._withdrawal_deadline = asyncio.get_running_loop().time() - 1
    assert await asyncio.wait_for(owner._drain_engine(withdrawal=True), 0.1) is False
    result = await asyncio.wait_for(handle.outcome(), 0.1)
    assert result.error_code == "engine_withdrawn" and result.model_id == "model-a"
    assert result.engine_release == "model-hub-engine-v8.0.63-2"
    assert not owner._engine_admission.idle.is_set()
    close_allowed.set()
    await asyncio.wait_for(owner._engine_admission.idle.wait(), 0.1)
    await owner._engine_admission.join_interruptions()


@pytest.mark.asyncio
async def test_recalled_process_stops_before_slow_fallback_stage(tmp_path, monkeypatch):
    from tests.engine_channel_support import manifest_bytes, release_entry, signed, tombstone
    from tests.test_model_hub_engine_lifecycle import owner_fixture, signed_candidate
    from vibe.model_hub_runtime.journal import JournalError

    owner, supervisor, _baseline = owner_fixture(tmp_path, monkeypatch)
    owner.ensure_running()
    candidate, signer, manifest, _envelope = signed_candidate(owner, tmp_path)
    owner.activate(candidate)
    fallback = manifest_bytes(version="v8.0.30", build=1)
    withdrawn = signed(signer, sequence=11, releases=[release_entry(manifest), release_entry(fallback)], revoked=[
        tombstone(candidate.asset_release_tag, "model-hub-engine-v8.0.30-1"),
    ])
    owner.channel._fetch = lambda: withdrawn
    adapter_owner = CLIProxyEngineAdapter(supervisor=supervisor, state_store=object())

    def slow_stage(_decision):
        assert supervisor.running is None
        value = owner.journal.load()
        assert value["pending"]["run_intent"] and value["blocked"] == "engine_withdrawn"
        raise JournalError("injected slow/unavailable download")

    monkeypatch.setattr(owner, "stage_decision", slow_stage)
    with pytest.raises(JournalError, match="injected"):
        await adapter_owner._reconcile_engine(explicit=False)
    assert supervisor.running is None
    assert adapter_owner._engine_admission.reason == "engine_withdrawn"


@pytest.mark.asyncio
async def test_known_withdrawal_gates_and_expires_while_metadata_publication_is_stuck(tmp_path, monkeypatch):
    import threading

    from core.handlers.model_hub.async_owner import run_owned_in_thread
    from tests.engine_channel_support import manifest_bytes, release_entry, signed, tombstone
    from tests.test_model_hub_engine_lifecycle import owner_fixture, signed_candidate
    from vibe.model_hub_runtime import adapter as module

    monkeypatch.setattr(module, "_ENGINE_WITHDRAWAL_DRAIN_SECONDS", 0.03)
    owner, supervisor, _baseline = owner_fixture(tmp_path, monkeypatch)
    candidate, signer, manifest, _envelope = signed_candidate(owner, tmp_path)
    owner.activate(candidate)
    withdrawn = signed(signer, sequence=11, releases=[
        release_entry(manifest), release_entry(manifest_bytes(version="v8.0.30", build=1)),
    ], revoked=[tombstone(candidate.asset_release_tag, "model-hub-engine-v8.0.30-1")])
    owner.channel._fetch = lambda: withdrawn
    adapter_owner = CLIProxyEngineAdapter(supervisor=supervisor, state_store=object())
    await adapter_owner.recover_lifecycle()
    work = adapter_owner._engine_admission.retain()
    signalled = asyncio.Event()
    work.abort = signalled.set
    adapter_owner._oauth_flows["fixture-flow"] = SimpleNamespace(lease=work)
    publication_allowed = threading.Event()
    monkeypatch.setattr(owner, "publish_status", lambda: publication_allowed.wait(5))

    @engine_operation
    async def fresh(self):
        pytest.fail("known withdrawn engine admitted fresh work")

    @engine_operation
    async def oauth_status(self, flow_id):
        return flow_id

    check = asyncio.create_task(run_owned_in_thread(owner.check))
    try:
        async def wait_gate():
            while adapter_owner._engine_admission.reason != "engine_withdrawn":
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_gate(), 1)
        assert not check.done()
        with pytest.raises(EngineUnavailableError, match="withdrawn"):
            await fresh(adapter_owner)
        assert await oauth_status(adapter_owner, "fixture-flow") == "fixture-flow"
        deadline = adapter_owner._withdrawal_deadline
        await asyncio.wait_for(signalled.wait(), 1)
        assert not check.done() and work.interrupted
        adapter_owner._gate_known_withdrawal()
        assert adapter_owner._withdrawal_deadline == deadline
        with pytest.raises(EngineUnavailableError):
            await oauth_status(adapter_owner, "fixture-flow")
    finally:
        publication_allowed.set()
        await check
        if adapter_owner._withdrawal_timer is not None:
            adapter_owner._withdrawal_timer.cancel()
        if adapter_owner._withdrawal_interrupt_task is not None:
            await adapter_owner._withdrawal_interrupt_task
        adapter_owner._engine_admission.release(work)


@pytest.mark.asyncio
async def test_interrupted_operation_cannot_create_a_new_transport_lease():
    class Owner:
        lifecycle = None
        _engine_admission = EngineAdmission()

        @engine_operation
        async def operation(self):
            self._engine_admission.close("engine_withdrawn")
            await self._engine_admission.interrupt()
            self._engine_admission.retain()

    owner = Owner()
    with pytest.raises(EngineUnavailableError) as refused:
        await owner.operation()
    assert refused.value.reason == "engine_withdrawn"
    assert owner._engine_admission.idle.is_set()


@pytest.mark.asyncio
async def test_synchronous_abort_release_cannot_drop_an_unjoined_async_close():
    gate = EngineAdmission()
    work = gate.retain()
    allowed = asyncio.Event()

    async def close():
        await allowed.wait()

    def abort():
        gate.release(work)
        return close()

    work.abort = abort
    gate.close("engine_withdrawn")
    await gate.interrupt()
    assert work.active and not gate.idle.is_set()
    allowed.set()
    await asyncio.wait_for(gate.idle.wait(), 1)
    await gate.join_interruptions()


@pytest.mark.asyncio
async def test_cancelled_release_stamp_joins_worker_without_leaking_transport(monkeypatch):
    import threading
    from unittest.mock import AsyncMock

    owner = adapter()
    source = SimpleNamespace(allowed_origins=(), protocol="openai_chat")
    owner.state_store = SimpleNamespace(get_source=lambda _sid: source)
    client = SimpleNamespace(invoke=AsyncMock())
    owner.supervisor.client = lambda: client
    monkeypatch.setattr(owner, "_locked_engine_call", AsyncMock(return_value=client))
    entered = asyncio.Event()
    allowed = threading.Event()
    loop = asyncio.get_running_loop()

    def stamp():
        loop.call_soon_threadsafe(entered.set)
        assert allowed.wait(5)
        return "model-hub-engine-v8.0.63-2"

    monkeypatch.setattr(owner, "engine_release", stamp)
    invocation = asyncio.create_task(owner.invoke("src_fixture", "model-a", {}, False, "vibey"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        invocation.cancel()
        await asyncio.sleep(0)
        assert not invocation.done()  # cancellation still joins the owned worker
    finally:
        allowed.set()
        with pytest.raises(asyncio.CancelledError):
            await invocation
    assert owner._engine_admission.idle.is_set()
    assert owner._active_transports == 0 and owner._transports_idle.is_set()
    assert owner._unsent_requests == 0 and owner._requests_sent.is_set()
    client.invoke.assert_not_awaited()
