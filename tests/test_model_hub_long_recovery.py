"""Long outages stay inside one request; only already-replayable failures wait.

The existing short retry tests do not cross a ten-minute outage. These drive the
real engine client and gateway with a fake upstream and injected admission clock;
restoring the old two-minute policy must end the request before it can succeed.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging

from aiohttp import web
import pytest

from config.v2_config import ModelHubSourceStateConfig
from core.handlers.model_hub.adapter import RawOutcomeKind
from core.handlers.model_hub.retry import RECOVERY_EXHAUSTED_CODE, RECOVERY_EXHAUSTED_MESSAGE
from core.handlers.model_hub.service import ModelHubError
from tests.test_model_hub_l3 import _outcome, _source
from tests.test_model_hub_retry_lifecycle import BACKENDS, configured_service, gateway_client, loopback_engine
from tests.test_model_hub_retry_policy import WIRE, clock_service, fail


@pytest.mark.parametrize("backend,ending", [
    (backend, ending) for backend in BACKENDS for ending in ("success", "exhausted", "stop")
    if (backend, ending) != ("opencode", "stop")
])
@pytest.mark.parametrize("reason", ["network", "server_error"])
async def test_long_outage_keeps_one_gateway_request_alive(tmp_path, caplog, backend, reason, ending):
    """MH-RETRY-LONG-001: recover at 10m, exhaust near 30m, or stop a live wait.

    Stop uses an attributed gateway Turn, not just cancellation of the test's
    HTTP client. Legacy uncorrelated OpenCode scopes have no such Turn owner.
    """
    service, clock, models = configured_service(tmp_path)
    calls = []
    waiting = asyncio.Event()
    original_sleep = service.recovery.sleep

    async def sleep(seconds):
        if ending == "stop" and clock.elapsed >= 600:
            waiting.set()
            await asyncio.Future()
        await original_sleep(seconds)

    service.recovery.sleep = sleep
    body = WIRE[BACKENDS[backend][1]][3]

    async def respond(request):
        calls.append(await request.json())
        if clock.elapsed >= 600 and ending == "success":
            return web.Response(body=body, content_type="application/json")
        if reason == "network":
            # A clean pre-output EOF is a retryable upstream disconnect. Losing
            # the local engine socket itself is engine_down, not Source health.
            return web.Response(body=b"", content_type="text/event-stream")
        return web.json_response({"error": {"type": "server_error"}}, status=503)

    with caplog.at_level(logging.INFO, logger="core.handlers.model_hub.service"):
        async with loopback_engine(tmp_path, service, respond) as (adapter, _client):
            async with gateway_client(service, backend, models[backend]) as (gateway, client, url, headers):
                task = asyncio.create_task(client.post(url, headers=headers, json={"model": "shared-model"}))
                try:
                    if ending == "stop":
                        await asyncio.wait_for(waiting.wait(), 5)
                        snapshot, = gateway.correlation.recovery_snapshot("turn_lifecycle")
                        assert snapshot["phase"] == "waiting"
                        assert snapshot["reason"] == reason
                        assert (
                            datetime.fromisoformat(snapshot["window_end"])
                            - datetime.fromisoformat(snapshot["started_at"])
                        ).total_seconds() == 1800
                        attempts = len(calls)
                        drain = gateway.finalize_turn("turn_lifecycle", settled_by="stopped", finish=lambda: None)
                        assert drain is not None
                        await asyncio.wait_for(drain, 2)
                        await asyncio.gather(task, return_exceptions=True)
                        assert len(calls) == attempts
                        assert not service._mutation_lock.locked()
                        assert service.recovery._sources[service.store.load().sources[0].id].owner is None
                    else:
                        response = await asyncio.wait_for(task, 5)
                        if ending == "success":
                            assert response.status == 200 and await response.read() == body
                            assert 600 <= clock.elapsed < 720
                        else:
                            assert response.status == (400 if backend == "codex" else 424)
                            assert "Retry-After" not in response.headers
                            assert (await response.json())["error"] == {
                                "type": RECOVERY_EXHAUSTED_CODE,
                                "code": RECOVERY_EXHAUSTED_CODE,
                                "message": RECOVERY_EXHAUSTED_MESSAGE,
                                "cause": reason,
                            }
                            # Do not wait when no further attempt fits inside the window.
                            assert 1680 <= clock.elapsed < 1800
                    await gateway._drain_turn_requests("turn_lifecycle")
                    assert gateway.correlation.recovery_snapshot("turn_lifecycle") == []
                    assert adapter._active_transports == 0
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
    assert len(calls) > 3 and all(call == calls[0] for call in calls)
    waits = [record for record in caplog.records if hasattr(record, "wait_seconds")]
    assert len(waits) == len(calls) - (ending != "stop")
    assert sum(record.wait_seconds for record in waits) == pytest.approx(clock.elapsed)
    assert {record.reason for record in waits} == {reason}
    assert {record.turn_id for record in waits} == ({None} if backend == "opencode" else {"turn_lifecycle"})
    assert waits[-1].outcome == ("canceled" if ending == "stop" else "retry_due")
    assert [record.attempt_count for record in waits] == list(range(1, len(waits) + 1))


@pytest.mark.parametrize("reason,cap,lower", [("network", 60, 48), ("server_error", 120, 96)])
async def test_transient_jitter_still_varies_at_the_cap(tmp_path, reason, cap, lower):
    """A long capped streak must not collapse to a fixed delay across instances."""
    service, clock = clock_service(tmp_path)
    source = service.store.load().sources[0]
    for _ in range(8):
        await fail(service, source, reason)
        clock.advance(cap)
    assert service.recovery._sources[source.id].retry_at < clock.now()
    for sample, expected in [(lambda low, high: low, lower), (lambda low, high: high, cap)]:
        service.recovery.jitter = sample
        await fail(service, source, reason)
        retry_at = service.recovery.annotations(service.store.load())[source.id].retry_at
        assert (datetime.fromisoformat(retry_at) - clock.now()).total_seconds() == expected
        clock.advance(cap)


@pytest.mark.parametrize("first_reason", [None, "network", "server_error"])
@pytest.mark.parametrize("status,code,waits", [(429, None, [60]), (429, "quota_exhausted", []), (400, "invalid_request_error", [])])
async def test_limits_and_terminal_failures_do_not_inherit_long_window(tmp_path, first_reason, status, code, waits):
    """Changing failure class must not give rate/quota an extended retry budget."""
    source_id = "src_recovery01"
    outcomes = [
        _outcome(RawOutcomeKind.HTTP_ERROR, status=status, code=code, source_id=source_id)
        for _ in range(4)
    ]
    prefix = []
    if first_reason:
        outcomes.insert(0, _outcome(
            RawOutcomeKind.NETWORK_ERROR if first_reason == "network" else RawOutcomeKind.HTTP_ERROR,
            status=None if first_reason == "network" else 503, source_id=source_id,
        ))
        prefix = [1 if first_reason == "network" else 30]
    service, clock = clock_service(tmp_path, outcomes=outcomes)
    with pytest.raises(ModelHubError) as ended:
        await service.resolve_with_recovery(backend="codex", model_id="shared-model", request={})
    assert ended.value.code == ("upstream_request_invalid" if status == 400 else RECOVERY_EXHAUSTED_CODE)
    # Failure streaks are Source-scoped, not per class: the next rate delay
    # after a transient failure is already 120s and cannot fit the old window.
    expected_waits = prefix + ([] if first_reason else waits)
    assert clock.delays == expected_waits
    assert len(service.adapter.invocations) == len(expected_waits) + 1


@pytest.mark.parametrize("mode", ["cooldown", "owned"])
@pytest.mark.parametrize("transient_first", [False, True])
@pytest.mark.parametrize("limit_reason", ["rate_limited", "quota_exhausted"])
async def test_short_budget_peer_cannot_starve_viable_transient_recovery(
    tmp_path, mode, transient_first, limit_reason,
):
    """MH-RETRY-LONG-001: choose admissible waits/owners, not the earliest short deadline."""
    limited, transient = _source("src_limited01", "Limited"), _source("src_transient1", "Transient")
    service, clock = clock_service(
        tmp_path, sources=[transient, limited] if transient_first else [limited, transient],
        outcomes=[_outcome(RawOutcomeKind.SUCCESS, source_id=transient.id, stream_started=True)],
    )
    owners = {}
    if mode == "cooldown":
        for source, reason, seconds in [(limited, limit_reason, 120), (transient, "server_error", 180)]:
            source.state = ModelHubSourceStateConfig(
                status="cooldown", retry_at=(clock.now() + timedelta(seconds=seconds)).isoformat(),
                detail_key=f"models.source.cooldown.{reason}",
            )
    else:
        await fail(service, limited, limit_reason)
        await fail(service, transient, "server_error")
        clock.advance(300)
        for source in (limited, transient):
            generation = service._reserve_settlement_generation(source.id)
            assert service.recovery.claim(source, generation)
            owners[source.id] = generation

        async def peer_finishes(seconds):
            clock.delays.append(seconds)
            clock.advance(min(seconds, 180))
            if seconds >= 180:
                service.recovery.release(transient.id, owners[transient.id])
            await asyncio.sleep(0)

        service.recovery.sleep = peer_finishes
    started = clock.elapsed
    result = await service.resolve_with_recovery(backend="codex", model_id="shared-model", request={})
    assert result.source_id == transient.id
    assert clock.elapsed - started == 180
    assert [source_id for source_id, *_ in service.adapter.invocations] == [transient.id]
    if mode == "owned":
        assert service.recovery._sources[limited.id].owner == owners[limited.id]


@pytest.mark.parametrize("limit_reason", ["rate_limited", "quota_exhausted"])
@pytest.mark.parametrize("eligible_at", [100, 180])
async def test_long_attempt_cannot_admit_a_short_budget_peer_after_its_deadline(tmp_path, limit_reason, eligible_at):
    """Wait selection is not enough: final admission must recheck the hop's own budget."""
    limited, transient = _source("src_limited01", "Limited"), _source("src_transient1", "Transient")
    service, clock = clock_service(tmp_path, sources=[limited, transient], outcomes=[
        _outcome(RawOutcomeKind.HTTP_ERROR, status=503, source_id=transient.id),
        _outcome(RawOutcomeKind.HTTP_ERROR, status=503, source_id=transient.id),
        _outcome(RawOutcomeKind.SUCCESS, source_id=transient.id, stream_started=True),
    ])
    limited.state = ModelHubSourceStateConfig(
        status="cooldown", retry_at=(clock.now() + timedelta(seconds=eligible_at)).isoformat(),
        detail_key=f"models.source.cooldown.{limit_reason}",
    )
    original_invoke = service.adapter.invoke

    async def slow_second_attempt(*args, **kwargs):
        handle = await original_invoke(*args, **kwargs)
        if len(service.adapter.invocations) == 2:
            clock.advance(150)
        return handle

    service.adapter.invoke = slow_second_attempt
    result = await service.resolve_with_recovery(backend="codex", model_id="shared-model", request={})
    assert result.source_id == transient.id
    assert clock.elapsed == 240
    assert clock.delays == [30, 60]
    assert [source_id for source_id, *_ in service.adapter.invocations] == [transient.id] * 3


@pytest.mark.parametrize("reason,observed", [("rate_limited", True), ("rate_limited", False), ("quota_exhausted", False)])
async def test_expired_limit_cannot_shorten_a_subsequent_network_outage(tmp_path, reason, observed):
    """Only an active persisted cooldown outranks a newly observed network failure."""
    source_id = "src_recovery01"
    failures = [_outcome(RawOutcomeKind.NETWORK_ERROR, source_id=source_id) for _ in range(20)]
    if observed:
        failures.insert(0, _outcome(RawOutcomeKind.HTTP_ERROR, status=429, source_id=source_id))
    service, clock = clock_service(tmp_path, outcomes=[
        *failures, _outcome(RawOutcomeKind.SUCCESS, source_id=source_id, stream_started=True),
    ])
    if not observed:
        service.store.load().sources[0].state = ModelHubSourceStateConfig(
            status="cooldown", retry_at=clock.now().isoformat(),
            detail_key=f"models.source.cooldown.{reason}",
        )
    result = await service.resolve_with_recovery(backend="codex", model_id="shared-model", request={})
    assert result.source_id == source_id
    assert 600 <= clock.elapsed < 1800
    assert len(service.adapter.invocations) == len(failures) + 1
