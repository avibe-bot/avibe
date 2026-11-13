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
@pytest.mark.parametrize("cancel_at", ["client", "inventory", "request"])
@pytest.mark.parametrize("withdrawal", [False, True], ids=["user_cancel", "withdrawal"])
async def test_oauth_start_cancellation_releases_unregistered_provider(tmp_path, monkeypatch, cancel_at, withdrawal):
    import threading

    from vibe.model_hub_runtime import adapter as module
    from vibe.model_hub_runtime.state import EngineStateStore

    entered = threading.Event()
    finish = threading.Event()
    state = EngineStateStore(tmp_path / "state")

    def pause(phase):
        if phase == cancel_at:
            entered.set()
            assert finish.wait(5)

    class Client:
        def management_request(self, *_args, **_kwargs):
            pause("request")
            return {"state": "fixture-state", "url": "https://oauth.example.test"}

    client = Client()

    def get_client():
        pause("client")
        return client

    def inventory(_client):
        pause("inventory")
        return {}

    monkeypatch.setattr(module, "_auth_inventory", inventory)
    owner = CLIProxyEngineAdapter(supervisor=SimpleNamespace(client=get_client), state_store=state)
    operation = asyncio.create_task(owner.start_oauth("src_fixture123", "anthropic"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert owner._active_oauth_providers == {"claude"}
        if withdrawal:
            owner._engine_admission.close("engine_withdrawn")
            await owner._engine_admission.interrupt()
        else:
            operation.cancel()
        # The thread must finish before its lease/reservation can be released.
        await asyncio.sleep(0)
        assert not operation.done()
        finish.set()
        with pytest.raises(EngineUnavailableError if withdrawal else asyncio.CancelledError):
            await asyncio.wait_for(operation, 5)
        assert owner._active_oauth_providers == set()
        assert owner._oauth_flows == {}
        assert owner._engine_admission.idle.is_set()
        owner._engine_admission.reopen()
        flow = await owner.start_oauth("src_fixture123", "anthropic")
        assert flow.flow_id in owner._oauth_flows
        owner._release_provider(owner._oauth_flows[flow.flow_id])
        assert owner._engine_admission.idle.is_set()
    finally:
        finish.set()
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.asyncio
async def test_terminal_oauth_failure_remains_visible_to_service_while_withdrawal_blocks_engine(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from core.handlers.model_hub.adapter import RetainedMaterialDisposition
    from tests.test_model_hub_api import _service
    from vibe.model_hub_runtime.state import EngineStateStore

    calls = []

    def management_request(_method, path, **_kwargs):
        calls.append(path)
        return {"files": []} if path == "/auth-files" else {"state": "fixture-state", "flow": "device"}

    client = SimpleNamespace(management_request=management_request)
    owner = CLIProxyEngineAdapter(
        supervisor=SimpleNamespace(client=lambda: client), state_store=EngineStateStore(tmp_path / "engine"),
    )
    service, _store, fake = _service(tmp_path)
    monkeypatch.setattr(fake, "start_oauth", owner.start_oauth)
    monkeypatch.setattr(fake, "oauth_status", owner.oauth_status)
    started = await service.oauth_start({"vendor": "openai", "channel": "hub"})
    flow_id = started["flow"]["flow_id"]
    flow = owner._oauth_flows[flow_id]
    owner._engine_admission.close("engine_withdrawn")
    await owner._engine_admission.interrupt()
    assert flow.state == "failed" and not flow.lease.active
    assert flow.retained_material_disposition is RetainedMaterialDisposition.UNKNOWN
    assert owner._engine_admission.idle.is_set()
    calls_before = list(calls)
    owner.lifecycle = SimpleNamespace(recovered=False)
    owner.recover_lifecycle = AsyncMock(side_effect=AssertionError("terminal inspection attempted recovery"))
    materialize = service._materialize_completed_oauth
    settled = []

    async def observe_materialization(*args):
        settled.append(args[2])
        return await materialize(*args)

    monkeypatch.setattr(service, "_materialize_completed_oauth", observe_materialization)
    result = await service.oauth_status(flow_id)
    assert result["flow"]["state"] == "failed"
    assert settled[0].retained_material_disposition is RetainedMaterialDisposition.UNKNOWN
    assert settled[0].error_key == "models.engine.withdrawn"
    assert calls == calls_before
    owner.recover_lifecycle.assert_not_awaited()
    with pytest.raises(EngineUnavailableError):
        owner._engine_admission.acquire()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["success", "failed", "cancelled"])
async def test_terminal_oauth_snapshots_never_take_a_new_execution_lease(state):
    owner = adapter()
    expected = object()
    owner._oauth_flows["flow"] = SimpleNamespace(state=state, snapshot=lambda: expected)
    owner._engine_admission.close("engine_withdrawn")
    assert await owner.oauth_status("flow") is expected
    assert owner._engine_admission.idle.is_set()
    with pytest.raises(EngineUnavailableError):
        await owner.submit_oauth("flow", "code")


@pytest.mark.asyncio
async def test_oauth_becoming_terminal_during_recovery_is_still_inspectable():
    from unittest.mock import AsyncMock

    owner = adapter()
    expected = object()
    flow = SimpleNamespace(state="awaiting_action", snapshot=lambda: expected)
    owner._oauth_flows["flow"] = flow
    owner.lifecycle = SimpleNamespace(withdrawn_current=False)

    async def recover():
        flow.state = "failed"
        owner._engine_admission.close("engine_withdrawn")

    owner.recover_lifecycle = AsyncMock(side_effect=recover)
    assert await owner.oauth_status("flow") is expected
    assert owner._engine_admission.idle.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_path", ["update", "doctor"])
async def test_explicit_retry_recovers_a_quarantined_required_baseline_without_an_adoption(tmp_path, monkeypatch, retry_path):
    from unittest.mock import AsyncMock

    from tests.test_model_hub_engine_lifecycle import owner_fixture
    from vibe.model_hub_runtime.journal import JournalError

    owner, supervisor, baseline = owner_fixture(tmp_path, monkeypatch)
    tag = baseline["generation"]["asset_release_tag"]
    supervisor.fail_tag = tag
    with pytest.raises(EngineUnavailableError):
        owner.ensure_running()
    assert owner.journal.load()["blocked"] == "engine_rollback_failed"
    assert tag in owner.journal.load()["quarantine"]
    owner.check(refresh=False)
    with pytest.raises(JournalError):
        owner.stage_required()
    assert tag in owner.journal.load()["quarantine"]
    supervisor.fail_tag = None
    supervisor.installer = owner.manager
    transport = CLIProxyEngineAdapter(supervisor=supervisor, state_store=object())
    transport.recover_lifecycle = AsyncMock()
    transport.status = AsyncMock(return_value=object())
    check = owner.check
    monkeypatch.setattr(owner, "check", lambda **kwargs: check(refresh=False, **kwargs))
    if retry_path == "update":
        await transport._reconcile_engine(explicit=True)
    else:
        await transport.ensure_installed(force=True)
    value = owner.journal.load()
    assert tag not in value["quarantine"]
    assert value["active"]["asset_release_tag"] == tag
    assert value["blocked"] is None and owner._ready
    assert value["health"] == "preflight_only" and supervisor.running is None
    owner.ensure_running()
    assert supervisor.running == owner.journal.load()["active"]


@pytest.mark.asyncio
async def test_bad_refresh_cannot_reopen_admission_after_unpublished_withdrawal(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from config import atomic_io
    from tests.engine_channel_support import manifest_bytes, release_entry, signed, tombstone
    from tests.test_model_hub_engine_lifecycle import owner_fixture, signed_candidate

    owner, supervisor, _baseline = owner_fixture(tmp_path, monkeypatch)
    candidate, signer, manifest, _envelope = signed_candidate(owner, tmp_path)
    owner.activate(candidate)
    owner.ensure_running()
    supervisor.installer = owner.manager
    transport = CLIProxyEngineAdapter(supervisor=supervisor, state_store=object())
    transport.status = AsyncMock(return_value=object())
    await transport.recover_lifecycle()
    withdrawn = signed(signer, 11, releases=[
        release_entry(manifest), release_entry(manifest_bytes(version="v8.0.30", build=1)),
    ], revoked=[tombstone(candidate.asset_release_tag, "model-hub-engine-v8.0.30-1")])
    replace = atomic_io.replace_snapshot

    def refuse_channel_publication(source, destination):
        if destination == owner.channel.store.state_path:
            raise OSError("test-owned channel publication refused")
        return replace(source, destination)

    monkeypatch.setattr(atomic_io, "replace_snapshot", refuse_channel_publication)
    owner.channel._fetch = lambda: withdrawn
    await transport._reconcile_engine(explicit=False)
    assert transport._engine_admission.reason == "engine_withdrawn"
    assert supervisor.running is None
    starts = sum(event == "start" for event, _tag in supervisor.events)

    owner.channel._fetch = lambda: b"{}"
    await transport._reconcile_engine(explicit=False)
    assert transport._engine_admission.reason == "engine_withdrawn"
    with pytest.raises(EngineUnavailableError):
        transport._engine_admission.acquire()
    assert supervisor.running is None
    assert sum(event == "start" for event, _tag in supervisor.events) == starts


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


@pytest.mark.asyncio
async def test_operation_lease_and_result_stamp_survive_later_selection():
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
    adapter_owner._oauth_flows["fixture-flow"] = SimpleNamespace(lease=work, state="awaiting_action")
    loop = asyncio.get_running_loop()
    publication_entered = asyncio.Event()
    publication_allowed = threading.Event()

    def blocked_publication():
        # The worker queued the real withdrawal callback before reaching this
        # publisher. FIFO loop callbacks make this a completion signal, not a
        # one-second throughput assertion over verification and filesystem I/O.
        loop.call_soon_threadsafe(publication_entered.set)
        publication_allowed.wait()  # released and joined on every exit below

    monkeypatch.setattr(owner, "publish_status", blocked_publication)
    scheduled = []
    call_at = loop.call_at

    def capture_expiry(when, callback, *args, context=None):
        if callback == adapter_owner._expire_withdrawal:
            # Keep the actual scheduled callback/arguments, but advance this
            # one timer explicitly after checking the admitted continuation.
            handle = asyncio.TimerHandle(when, callback, args, loop, context=context)
            scheduled.append((when, callback, args, handle))
            return handle
        return call_at(when, callback, *args, context=context)

    monkeypatch.setattr(loop, "call_at", capture_expiry)

    @engine_operation
    async def fresh(self):
        pytest.fail("known withdrawn engine admitted fresh work")

    @engine_operation
    async def oauth_status(self, flow_id):
        return flow_id

    check = asyncio.create_task(run_owned_in_thread(owner.check))
    entered = asyncio.create_task(publication_entered.wait())
    try:
        # Only a deadlock watchdog: progress is the publisher's signal, and a
        # worker that exits early must not look like blocked publication.
        done, _pending = await asyncio.wait({check, entered}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
        assert entered in done, "check exited or stalled before reaching status publication"
        assert not check.done()
        assert adapter_owner._engine_admission.reason == "engine_withdrawn"
        with pytest.raises(EngineUnavailableError, match="withdrawn"):
            await fresh(adapter_owner)
        assert await oauth_status(adapter_owner, "fixture-flow") == "fixture-flow"
        deadline = adapter_owner._withdrawal_deadline
        assert not signalled.is_set() and not work.interrupted
        assert len(scheduled) == 1
        when, expire, args, handle = scheduled[0]
        assert when == deadline and args == (deadline,)
        assert adapter_owner._withdrawal_timer is handle and not handle.cancelled()
        expire(*args)
        assert adapter_owner._withdrawal_interrupt_task is not None
        await asyncio.wait_for(signalled.wait(), 30)
        assert not check.done() and work.interrupted
        adapter_owner._gate_known_withdrawal()
        assert adapter_owner._withdrawal_deadline == deadline and len(scheduled) == 1
        with pytest.raises(EngineUnavailableError):
            await oauth_status(adapter_owner, "fixture-flow")
    finally:
        publication_allowed.set()
        entered.cancel()
        await asyncio.gather(entered, return_exceptions=True)
        try:
            await check
        finally:
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
