from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from core.handlers.model_hub.adapter import RawOutcomeKind
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from core.run_settlement import (
    SETTLED_BY_BACKEND_REFRESH, SETTLED_BY_NO_TERMINAL_RESULT,
    SETTLED_BY_STOPPED, SETTLED_BY_TERMINAL_RESULT,
)
from modules.agents.model_hub import ModelHubRuntimeRouter
from modules.agents.codex.event_handler import CodexEventHandler
from tests.test_model_hub_l3 import (
    FakeStreamResponse,
    LiveInvokeHandle,
    NeverResolvingCloseHandle,
    _canonicalize_fixed_test_routes,
    _outcome,
    _prepared_gateway_request,
    _service,
    _source,
)


TERMINAL = (
    b'event: response.completed\ndata: {"type":"response.completed",'
    b'"response":{"status":"completed","output":[],"usage":'
    b'{"input_tokens":1,"output_tokens":1}}}\n\n'
)


async def _settle(runtime, turn_id, settled_by=SETTLED_BY_TERMINAL_RESULT):
    completion = runtime.settle_turn(
        turn_id, settled_by=settled_by, ts="2026-09-06T00:00:00+00:00"
    )
    if inspect.isawaitable(completion):
        await completion


class HeldTerminalHandle(LiveInvokeHandle):
    def __init__(self, source_id, chunks=(TERMINAL,)):
        super().__init__(
            _outcome(RawOutcomeKind.SUCCESS, status=200, source_id=source_id, stream_started=True),
            chunks,
        )
        self.at_end = asyncio.Event()
        self.release = asyncio.Event()

    async def _iterate(self, chunks):
        for chunk in chunks:
            yield chunk
        self.at_end.set()
        await self.release.wait()


@pytest.mark.parametrize("early", [True, False], ids=["terminal-frame", "gateway-finished"])
def test_loopback_first_error_then_success_retained_on_reused_credentials(tmp_path, early):
    async def exercise():
        source = _source("src_retention01", "Retention")
        handle = HeldTerminalHandle(source.id)
        service = _service(
            tmp_path,
            sources=[source],
            outcomes=[_outcome(RawOutcomeKind.HTTP_ERROR, status=404, code="model_not_found", source_id=source.id)],
        )
        model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service)
        runtime = ModelHubRuntimeRouter(service=service, turn_gateway=gateway, overlay_path=tmp_path / "overlay.json")
        first = _prepared_gateway_request(
            gateway, turn_id="first-error", requested_model=model, source_id=source.id, stream=True
        )
        completion = None
        try:
            await gateway._handle_request(first)
            await _settle(runtime, "first-error")
            assert service.provenance.get("first-error")["terminal_error"]["upstream_error_code"] == "model_not_found"
            service.adapter.live_handles.append(handle)
            base, token = await gateway.endpoint(
                "codex", process_scope="/repo", turn_id="second-success", requested_model_id=model,
                resolved_model_id="shared-model", source_id=source.id,
            )
            assert first.headers["Authorization"] == f"Bearer {token}"
            async with aiohttp.ClientSession(trust_env=False) as client:
                async with client.post(
                    f"{base}/v1/responses", headers={"Authorization": f"Bearer {token}"},
                    json={"model": "shared-model", "input": "synthetic", "stream": True},
                ) as response:
                    frame = await asyncio.wait_for(response.content.readuntil(b"\n\n"), 5)
                    assert b"response.completed" in frame
                    await asyncio.wait_for(handle.at_end.wait(), 5)
                    if early:
                        completion = asyncio.create_task(_settle(runtime, "second-success"))
                        await asyncio.sleep(0)
                    handle.release.set()
                    await asyncio.wait_for(response.read(), 5)
                    if completion is None:
                        completion = asyncio.create_task(_settle(runtime, "second-success"))
                    await asyncio.wait_for(completion, 5)
            latest = service.provenance.latest_for_model("codex", model)
            assert latest["turn_id"] == "second-success"
            assert latest["outcome"] == "served"
            assert latest["terminal_error"] is None
            assert len(service.adapter.invocations) == 2
        finally:
            handle.release.set()
            if completion is not None:
                await asyncio.gather(completion, return_exceptions=True)
            await gateway.close()

    asyncio.run(exercise())


def test_codex_will_retry_keeps_same_turn_open_until_success_settles(tmp_path):
    async def exercise():
        source = _source("src_retryturn1", "Retry")
        handle = HeldTerminalHandle(source.id)
        service = _service(tmp_path, sources=[source], outcomes=[
            _outcome(RawOutcomeKind.HTTP_ERROR, status=404, code="model_not_found", source_id=source.id)
        ])
        model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service)
        runtime = ModelHubRuntimeRouter(service=service, turn_gateway=gateway, overlay_path=tmp_path / "overlay.json")
        request = _prepared_gateway_request(
            gateway, turn_id="retried", requested_model=model, source_id=source.id, stream=True
        )
        recorder = AsyncMock()
        handler = CodexEventHandler(SimpleNamespace(_record_model_hub_native_failure=recorder))
        running = completion = None
        try:
            await gateway._handle_request(request)
            await handler.handle_notification(
                "error", {"error": {"message": "synthetic retry"}, "willRetry": True, "turnId": "retried"},
                SimpleNamespace(context=SimpleNamespace(platform_specific={"turn_token": "retried"})),
            )
            recorder.assert_not_awaited()
            assert service.provenance.get("retried") is None
            retry = _prepared_gateway_request(
                gateway, turn_id="retried", requested_model=model, source_id=source.id, stream=True
            )
            assert request.headers == retry.headers
            service.adapter.live_handles.append(handle)
            with patch("core.handlers.model_hub.turn_gateway.web.StreamResponse", return_value=FakeStreamResponse()):
                running = asyncio.create_task(gateway._handle_request(retry))
                await asyncio.wait_for(handle.at_end.wait(), 5)
                completion = runtime.settle_turn(
                    "retried", settled_by=SETTLED_BY_TERMINAL_RESULT, ts="2026-09-06T00:00:00Z"
                )
                assert completion is not None and not completion.done()
                handle.release.set()
                await asyncio.wait_for(completion, 5)
                await running
            record = service.provenance.get("retried")
            assert record["outcome"] == "served"
            assert record["terminal_error"] is None
            assert record["served"]["source_id"] == source.id
            assert len(service.adapter.invocations) == 2
        finally:
            handle.release.set()
            await asyncio.gather(*(task for task in (running, completion) if task is not None), return_exceptions=True)
            await gateway.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("malformed", [False, True], ids=["valid", "malformed"])
def test_request_admitted_before_completion_keeps_identity_through_body_parse(tmp_path, malformed):
    async def exercise():
        source = _source("src_parsedturn", "Delayed body")
        service = _service(tmp_path, sources=[source], live_handles=[
            LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, status=200, source_id=source.id), (TERMINAL,))
        ])
        model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service)
        runtime = ModelHubRuntimeRouter(service=service, turn_gateway=gateway, overlay_path=tmp_path / "overlay.json")
        request = _prepared_gateway_request(gateway, turn_id="parsed", requested_model=model, source_id=source.id, stream=True)
        original_json = request.json
        parsing = asyncio.Event()
        release = asyncio.Event()

        async def parse(**_kwargs):
            parsing.set()
            await release.wait()
            return {} if malformed else await original_json()

        request.json = parse
        completion = None
        running = None
        try:
            with patch("core.handlers.model_hub.turn_gateway.web.StreamResponse", return_value=FakeStreamResponse()):
                running = asyncio.create_task(gateway._handle_request(request))
                await asyncio.wait_for(parsing.wait(), 5)
                completion = asyncio.create_task(_settle(runtime, "parsed"))
                await asyncio.sleep(0)
                assert not completion.done()
                # A later request may still route, but must not claim the closed turn.
                token = request.headers["Authorization"].removeprefix("Bearer ")
                with gateway.correlation.gateway_terminalizer(backend="codex", token=token) as late:
                    assert late.turn_id is None
                release.set()
                await asyncio.wait_for(running, 5)
                await asyncio.wait_for(completion, 5)
            record = service.provenance.get("parsed")
            assert record["outcome"] == ("failed_terminal" if malformed else "served")
            if malformed:
                assert record["terminal_error"]["reason"] == "invalid_parameter"
                assert service.adapter.invocations == []
        finally:
            release.set()
            await asyncio.gather(*(task for task in (running, completion) if task is not None), return_exceptions=True)
            await gateway.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("exact_request", [False, True], ids=["untracked-only", "also-exact"])
@pytest.mark.parametrize("reason", [SETTLED_BY_TERMINAL_RESULT, SETTLED_BY_STOPPED])
def test_out_of_window_request_releases_finalization_ownership(tmp_path, exact_request, reason):
    async def exercise():
        source = _source("src_oldroute01", "Old route")
        old_handle = HeldTerminalHandle(source.id)
        exact_handle = HeldTerminalHandle(source.id)
        service = _service(tmp_path, sources=[source], live_handles=[old_handle, exact_handle])
        old_model = _canonicalize_fixed_test_routes(service)["codex"]
        agent = service.store.load().agents["codex"]
        new_model = next(model for model in agent.routes if model != old_model)
        agent.routes[new_model] = agent.routes[old_model]
        gateway = ModelHubTurnGateway(service, transport_timeout=0.02)
        runtime = ModelHubRuntimeRouter(service=service, turn_gateway=gateway, overlay_path=tmp_path / "overlay.json")
        old_request = _prepared_gateway_request(
            gateway, turn_id="old-route", requested_model=old_model, source_id=source.id, stream=True
        )
        await _settle(runtime, "old-route")
        new_request = _prepared_gateway_request(
            gateway, turn_id="new-route", requested_model=new_model, source_id=source.id, stream=True
        )
        assert old_request.headers != new_request.headers
        old_task = exact_task = completion = None
        try:
            with patch("core.handlers.model_hub.turn_gateway.web.StreamResponse", side_effect=lambda **_kwargs: FakeStreamResponse()):
                old_task = asyncio.create_task(gateway._handle_request(old_request))
                await asyncio.wait_for(old_handle.at_end.wait(), 5)
                if exact_request:
                    exact_task = asyncio.create_task(gateway._handle_request(new_request))
                    await asyncio.wait_for(exact_handle.at_end.wait(), 5)
                completion = runtime.settle_turn("new-route", settled_by=reason, ts="2026-09-06T00:00:00+00:00")
                if exact_request:
                    assert completion is not None
                    assert not completion.done()
                    if reason == SETTLED_BY_TERMINAL_RESULT:
                        exact_handle.release.set()
                    await asyncio.wait_for(asyncio.shield(completion), 5)
                    await asyncio.gather(exact_task, return_exceptions=True)
                    record = service.provenance.get("new-route")
                    assert record["outcome"] == ("served" if reason == SETTLED_BY_TERMINAL_RESULT else "canceled")
                    assert record["requested_model_id"] == new_model
                else:
                    assert completion is None
                    record = service.provenance.get("new-route")
                    if reason == SETTLED_BY_STOPPED:
                        # Existing no-request Stop history is retained, without
                        # attributing any of the out-of-window request's facts.
                        assert record["outcome"] == "canceled"
                        assert record["canceled_attempt"] is None
                        assert record["served"] is None
                        assert record["terminal_error"] is None
                        assert record["failed_attempts"] == []
                    else:
                        assert record is None
                assert not old_task.done(), "finalization must neither wait for nor cancel the untracked request"
                old_handle.release.set()
                await asyncio.wait_for(old_task, 5)
            assert service.provenance.get("old-route") is None
            assert len(service.adapter.invocations) == (2 if exact_request else 1)
        finally:
            old_handle.release.set()
            exact_handle.release.set()
            await asyncio.gather(*(task for task in (old_task, exact_task, completion) if task is not None), return_exceptions=True)
            await gateway.close()

    asyncio.run(exercise())


def test_body_claim_releases_request_already_snapshotted_by_finalization(tmp_path):
    async def exercise():
        source = _source("src_bodyclaim01", "Body claim")
        handle = HeldTerminalHandle(source.id)
        service = _service(tmp_path, sources=[source], live_handles=[handle])
        old_model = _canonicalize_fixed_test_routes(service)["codex"]
        agent = service.store.load().agents["codex"]
        new_model = next(model for model in agent.routes if model != old_model)
        gateway = ModelHubTurnGateway(service, transport_timeout=0.05)
        runtime = ModelHubRuntimeRouter(service=service, turn_gateway=gateway, overlay_path=tmp_path / "overlay.json")
        request = _prepared_gateway_request(
            gateway, turn_id="old-route", requested_model=old_model, source_id=source.id, stream=True
        )
        await _settle(runtime, "old-route")
        _prepared_gateway_request(gateway, turn_id="new-route", requested_model=new_model, source_id=source.id, stream=True)
        original_json = request.json
        parsing = asyncio.Event()
        release = asyncio.Event()
        draining = asyncio.Event()
        original_drain = gateway._drain_turn_requests

        async def parse(**kwargs):
            parsing.set()
            await release.wait()
            return await original_json(**kwargs)

        async def drain(*args, **kwargs):
            draining.set()
            await original_drain(*args, **kwargs)

        request.json = parse
        running = completion = None
        try:
            with patch("core.handlers.model_hub.turn_gateway.web.StreamResponse", return_value=FakeStreamResponse()), patch.object(
                gateway, "_drain_turn_requests", side_effect=drain
            ):
                running = asyncio.create_task(gateway._handle_request(request))
                await asyncio.wait_for(parsing.wait(), 5)
                completion = runtime.settle_turn(
                    "new-route", settled_by=SETTLED_BY_TERMINAL_RESULT, ts="2026-09-06T00:00:00+00:00"
                )
                await asyncio.wait_for(draining.wait(), 5)
                release.set()
                await asyncio.wait_for(handle.at_end.wait(), 5)
                await asyncio.wait_for(asyncio.shield(completion), 5)
                assert not running.done(), "released attribution must wake the existing drain without canceling transport"
                assert service.provenance.get("new-route") is None
                handle.release.set()
                await asyncio.wait_for(running, 5)
                assert service.provenance.get("old-route") is None
        finally:
            release.set()
            handle.release.set()
            await asyncio.gather(*(task for task in (running, completion) if task is not None), return_exceptions=True)
            await gateway.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "reason,backend_rejection,expected",
    [
        (SETTLED_BY_STOPPED, False, "canceled"),
        (SETTLED_BY_NO_TERMINAL_RESULT, False, "failed_terminal"),
        (SETTLED_BY_BACKEND_REFRESH, False, "failed_terminal"),
        (SETTLED_BY_TERMINAL_RESULT, True, "failed_terminal"),
    ],
)
def test_finalization_preserves_stop_and_backend_failure_before_late_success(tmp_path, reason, backend_rejection, expected):
    async def exercise():
        source = _source("src_stoppedturn", "Late success")
        handle = HeldTerminalHandle(source.id)
        service = _service(tmp_path, sources=[source], live_handles=[handle])
        model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service)
        runtime = ModelHubRuntimeRouter(service=service, turn_gateway=gateway, overlay_path=tmp_path / "overlay.json")
        request = _prepared_gateway_request(gateway, turn_id="terminated", requested_model=model, source_id=source.id, stream=True)
        running = None
        completion = None
        try:
            with patch("core.handlers.model_hub.turn_gateway.web.StreamResponse", return_value=FakeStreamResponse()):
                running = asyncio.create_task(gateway._handle_request(request))
                await asyncio.wait_for(handle.at_end.wait(), 5)
                if backend_rejection:
                    gateway.correlation.fail_hub_attempt("terminated")
                completion = asyncio.create_task(_settle(runtime, "terminated", reason))
                await asyncio.sleep(0)
                handle.release.set()
                await asyncio.gather(running, return_exceptions=True)
                await asyncio.wait_for(completion, 5)
            record = service.provenance.get("terminated")
            assert record["outcome"] == expected
            assert record["served"] is None
            if backend_rejection:
                assert record["terminal_error"]["reason"] == "protocol_error"
            elif reason != SETTLED_BY_STOPPED:
                assert record["terminal_error"]["reason"] == "stream_interrupted"
            else:
                assert record["canceled_attempt"]["source_id"] == source.id
        finally:
            handle.release.set()
            await asyncio.gather(*(task for task in (running, completion) if task is not None), return_exceptions=True)
            await gateway.close()

    asyncio.run(exercise())


def test_finalization_deadline_cancels_owned_request_without_inventing_success(tmp_path):
    async def exercise():
        source = _source("src_deadline01", "Deadline")
        service = _service(tmp_path, sources=[source])
        model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service, transport_timeout=0.02)
        runtime = ModelHubRuntimeRouter(service=service, turn_gateway=gateway, overlay_path=tmp_path / "overlay.json")
        request = _prepared_gateway_request(gateway, turn_id="deadline", requested_model=model, source_id=source.id, stream=True)
        entered = asyncio.Event()

        async def parse(**_kwargs):
            entered.set()
            await asyncio.Event().wait()

        request.json = parse
        running = asyncio.create_task(gateway._handle_request(request))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await asyncio.wait_for(_settle(runtime, "deadline"), 1)
            await asyncio.gather(running, return_exceptions=True)
            record = service.provenance.get("deadline")
            assert record is None or record["served"] is None
            assert service.adapter.invocations == []
            assert not gateway._turn_requests
            assert not gateway._turn_completions
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            await gateway.close()

    asyncio.run(exercise())


def test_completion_is_owned_once_through_repeated_waiter_cancellation(tmp_path):
    async def exercise():
        source = _source("src_cancelwait", "Canceled waiter")
        handle = HeldTerminalHandle(source.id)
        service = _service(tmp_path, sources=[source], live_handles=[handle])
        model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service)
        runtime = ModelHubRuntimeRouter(service=service, turn_gateway=gateway, overlay_path=tmp_path / "overlay.json")
        request = _prepared_gateway_request(gateway, turn_id="owned", requested_model=model, source_id=source.id, stream=True)
        puts = []
        original_put = service.provenance.put

        def put(record):
            puts.append(record)
            original_put(record)

        service.provenance.put = put
        running = None
        waiter = None
        try:
            with patch("core.handlers.model_hub.turn_gateway.web.StreamResponse", return_value=FakeStreamResponse()):
                running = asyncio.create_task(gateway._handle_request(request))
                await asyncio.wait_for(handle.at_end.wait(), 5)
                completion = runtime.settle_turn("owned", settled_by=SETTLED_BY_TERMINAL_RESULT, ts="2026-09-06T00:00:00Z")
                assert completion is runtime.settle_turn("owned", settled_by=SETTLED_BY_STOPPED, ts="later")

                async def wait():
                    await asyncio.shield(completion)

                for _ in range(2):
                    waiter = asyncio.create_task(wait())
                    await asyncio.sleep(0)
                    waiter.cancel()
                    await asyncio.gather(waiter, return_exceptions=True)
                    assert not completion.done()
                handle.release.set()
                await asyncio.wait_for(completion, 5)
                await running
                await _settle(runtime, "owned")
            assert len(puts) == 1
            assert puts[0]["outcome"] == "served"
        finally:
            handle.release.set()
            await asyncio.gather(*(task for task in (running, waiter) if task is not None), return_exceptions=True)
            await gateway.close()

    asyncio.run(exercise())


def test_finalization_waits_for_every_request_after_its_attempt_is_finished(tmp_path):
    async def exercise():
        source = _source("src_retention02", "Concurrent requests")
        service = _service(tmp_path, sources=[source], live_handles=[
            LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, status=200, source_id=source.id), (TERMINAL,))
            for _ in range(2)
        ])
        model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service)
        runtime = ModelHubRuntimeRouter(service=service, turn_gateway=gateway, overlay_path=tmp_path / "overlay.json")
        at_eof = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]

        class HeldEOF(FakeStreamResponse):
            def __init__(self, index):
                super().__init__()
                self.index = index

            async def write_eof(self):
                at_eof[self.index].set()
                await release[self.index].wait()

        requests = []
        completion = None
        try:
            with patch("core.handlers.model_hub.turn_gateway.web.StreamResponse", side_effect=[HeldEOF(0), HeldEOF(1)]):
                for _ in range(2):
                    request = _prepared_gateway_request(
                        gateway, turn_id="concurrent", requested_model=model, source_id=source.id, stream=True
                    )
                    requests.append(asyncio.create_task(gateway._handle_request(request)))
                await asyncio.wait_for(asyncio.gather(*(event.wait() for event in at_eof)), 5)
                completion = asyncio.create_task(_settle(runtime, "concurrent"))
                await asyncio.sleep(0)
                assert not completion.done()
                release[0].set()
                await requests[0]
                assert not completion.done()
                release[1].set()
                await asyncio.gather(*requests)
                await asyncio.wait_for(completion, 5)
            assert service.provenance.get("concurrent")["outcome"] == "served"
        finally:
            for event in release:
                event.set()
            await asyncio.gather(*requests, return_exceptions=True)
            if completion is not None:
                await asyncio.gather(completion, return_exceptions=True)
            await gateway.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("no_candidate", [False, True], ids=["malformed", "no-candidate"])
def test_noninvoking_request_must_exit_before_finalization(tmp_path, no_candidate):
    from config.v2_config import ModelHubRouteConfig

    async def exercise():
        source = _source("src_noinvoke01", "Non-invoking request")
        service = _service(tmp_path, sources=[source])
        model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service)
        runtime = ModelHubRuntimeRouter(service=service, turn_gateway=gateway, overlay_path=tmp_path / "overlay.json")
        request = _prepared_gateway_request(gateway, turn_id="noninvoking", requested_model=model, source_id=source.id, stream=True)
        if no_candidate:
            service.store.config.agents["codex"].routes[model] = ModelHubRouteConfig()
        else:
            request.json.return_value = {}
        response_ready = asyncio.Event()
        release = asyncio.Event()
        run_request = gateway._run_request_turn

        async def exiting(*args, **kwargs):
            response = await run_request(*args, **kwargs)
            response_ready.set()
            await release.wait()
            return response

        gateway._run_request_turn = exiting
        running = asyncio.create_task(gateway._handle_request(request))
        completion = None
        try:
            await asyncio.wait_for(response_ready.wait(), 5)
            completion = asyncio.create_task(_settle(runtime, "noninvoking"))
            await asyncio.sleep(0)
            assert not completion.done()
            assert service.provenance.get("noninvoking") is None
            release.set()
            await running
            await asyncio.wait_for(completion, 5)
            record = service.provenance.get("noninvoking")
            assert record["outcome"] == ("no_candidate" if no_candidate else "failed_terminal")
            assert service.adapter.invocations == []
        finally:
            release.set()
            await asyncio.gather(*(task for task in (running, completion) if task is not None), return_exceptions=True)
            await gateway.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("ending", ["stop", "retire", "close"])
def test_committed_gateway_facts_survive_completion_cleanup(tmp_path, ending):
    async def exercise():
        source = _source("src_committed1", "Committed attempt")
        service = _service(tmp_path, sources=[source], live_handles=[
            LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, status=200, source_id=source.id), (TERMINAL,))
        ])
        model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service)
        runtime = ModelHubRuntimeRouter(service=service, turn_gateway=gateway, overlay_path=tmp_path / "overlay.json")
        request = _prepared_gateway_request(gateway, turn_id="committed", requested_model=model, source_id=source.id, stream=True)
        at_eof = asyncio.Event()
        release = asyncio.Event()

        class HeldEOF(FakeStreamResponse):
            async def write_eof(self):
                at_eof.set()
                await release.wait()

        running = None
        completion = None
        try:
            with patch("core.handlers.model_hub.turn_gateway.web.StreamResponse", return_value=HeldEOF()):
                running = asyncio.create_task(gateway._handle_request(request))
                await asyncio.wait_for(at_eof.wait(), 5)
                completion = runtime.settle_turn(
                    "committed", settled_by=SETTLED_BY_STOPPED if ending == "stop" else SETTLED_BY_TERMINAL_RESULT,
                    ts="2026-09-06T00:00:00Z",
                )
                assert completion is not None
                if ending == "retire":
                    gateway.correlation.retire_scope("codex", "/repo")
                elif ending == "close":
                    await gateway.close()
                release.set()
                await asyncio.gather(running, return_exceptions=True)
                await asyncio.wait_for(completion, 5)
            assert service.provenance.get("committed")["outcome"] == "served"
            assert not gateway._turn_requests
            assert not gateway._turn_completions
        finally:
            release.set()
            await asyncio.gather(*(task for task in (running, completion) if task is not None), return_exceptions=True)
            await gateway.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("ending", ["retire", "overlap", "untracked"])
def test_ambiguous_owned_request_is_never_promoted_to_attributed_success(tmp_path, ending):
    async def exercise():
        source = _source("src_ambiguous1", "Ambiguous turn")
        handle = HeldTerminalHandle(source.id)
        service = _service(tmp_path, sources=[source], live_handles=[handle])
        model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service)
        runtime = ModelHubRuntimeRouter(service=service, turn_gateway=gateway, overlay_path=tmp_path / "overlay.json")
        request = _prepared_gateway_request(gateway, turn_id="ambiguous", requested_model=model, source_id=source.id, stream=True)
        running = None
        completion = None
        try:
            with patch("core.handlers.model_hub.turn_gateway.web.StreamResponse", return_value=FakeStreamResponse()):
                running = asyncio.create_task(gateway._handle_request(request))
                await asyncio.wait_for(handle.at_end.wait(), 5)
                completion = runtime.settle_turn("ambiguous", settled_by=SETTLED_BY_TERMINAL_RESULT, ts="2026-09-06T00:00:00Z")
                if ending == "retire":
                    gateway.correlation.retire_scope("codex", "/repo")
                else:
                    gateway.correlation.credentials("codex", "/repo", "peer" if ending == "overlap" else None)
                handle.release.set()
                await running
                await asyncio.wait_for(completion, 5)
            assert service.provenance.get("ambiguous") is None
        finally:
            handle.release.set()
            await asyncio.gather(*(task for task in (running, completion) if task is not None), return_exceptions=True)
            await gateway.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("failed", [False, True])
def test_native_only_and_untracked_turns_finalize_without_async_work(tmp_path, failed):
    source = _source("src_nativeonly", "Native", channel="native_cli")
    service = _service(tmp_path, sources=[source])
    gateway = ModelHubTurnGateway(service)
    runtime = ModelHubRuntimeRouter(service=service, turn_gateway=gateway, overlay_path=tmp_path / "overlay.json")
    gateway.correlation.begin_native_attempt(
        backend="codex", process_scope="/repo", turn_id="native", requested_model_id="shared-model",
        source_id=source.id, resolved_model_id="shared-model", via_mapping=False,
    )
    if failed:
        gateway.correlation.fail_native_attempt("native", reason="rate_limited")
    assert runtime.settle_turn("native", settled_by=SETTLED_BY_TERMINAL_RESULT, ts="2026-09-06T00:00:00Z") is None
    assert service.provenance.get("native")["outcome"] == ("exhausted" if failed else "served")
    gateway.correlation.credentials("opencode", "shared", "opencode")
    assert runtime.settle_turn("opencode", settled_by=SETTLED_BY_TERMINAL_RESULT, ts="2026-09-06T00:00:00Z") is None
    assert service.provenance.get("opencode") is None
    assert not gateway._turn_completions


def test_fallback_requests_keep_owned_identity_until_the_final_attempt_finishes(tmp_path):
    async def exercise():
        first = _source("src_fallback01", "First")
        second = _source("src_fallback02", "Second")
        handle = HeldTerminalHandle(second.id)
        service = _service(tmp_path, sources=[first, second], outcomes=[
            _outcome(RawOutcomeKind.HTTP_ERROR, status=429, source_id=first.id)
        ])
        model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service)
        runtime = ModelHubRuntimeRouter(service=service, turn_gateway=gateway, overlay_path=tmp_path / "overlay.json")
        original_invoke = service.adapter.invoke

        async def invoke(source_id, *args, **kwargs):
            if source_id == second.id:
                service.adapter.live_handles.append(handle)
            return await original_invoke(source_id, *args, **kwargs)

        service.adapter.invoke = invoke
        request = _prepared_gateway_request(gateway, turn_id="fallback", requested_model=model, source_id=first.id, stream=True)
        running = None
        completion = None
        try:
            with patch("core.handlers.model_hub.turn_gateway.web.StreamResponse", return_value=FakeStreamResponse()):
                running = asyncio.create_task(gateway._handle_request(request))
                await asyncio.wait_for(handle.at_end.wait(), 5)
                # Same-turn retry preparation remains idempotent, not overlapping.
                assert gateway.correlation.credentials("codex", "/repo", "fallback")
                completion = runtime.settle_turn("fallback", settled_by=SETTLED_BY_TERMINAL_RESULT, ts="2026-09-06T00:00:00Z")
                assert completion is not None
                handle.release.set()
                await running
                await asyncio.wait_for(completion, 5)
            record = service.provenance.get("fallback")
            assert record["outcome"] == "served"
            assert record["served"]["source_id"] == second.id
            assert [attempt["source_id"] for attempt in record["failed_attempts"]] == [first.id]
            assert len(service.adapter.invocations) == 2
        finally:
            handle.release.set()
            await asyncio.gather(*(task for task in (running, completion) if task is not None), return_exceptions=True)
            await gateway.close()

    asyncio.run(exercise())


def test_finalization_abandons_stuck_teardown_without_late_history_mutation(tmp_path):
    async def exercise():
        source = _source("src_stuckdrain", "Stuck teardown")
        handle = NeverResolvingCloseHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=source.id))
        service = _service(tmp_path, sources=[source], live_handles=[handle])
        model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service, transport_timeout=0.02)
        runtime = ModelHubRuntimeRouter(service=service, turn_gateway=gateway, overlay_path=tmp_path / "overlay.json")
        request = _prepared_gateway_request(gateway, turn_id="stuck", requested_model=model, source_id=source.id, stream=True)
        running = None
        try:
            with patch("core.handlers.model_hub.turn_gateway.web.StreamResponse", return_value=FakeStreamResponse()):
                running = asyncio.create_task(gateway._handle_request(request))
                await asyncio.wait_for(handle.started.wait(), 5)
                await asyncio.wait_for(_settle(runtime, "stuck"), 1)
                record = service.provenance.get("stuck")
                assert record["outcome"] == "failed_terminal"
                assert record["terminal_error"]["reason"] == "engine_down"
                assert gateway.resource_leak_records
                assert not gateway._turn_requests
                assert not gateway._turn_completions
                handle.release_close.set()
                await asyncio.wait_for(handle.closed.wait(), 1)
                await asyncio.gather(running, return_exceptions=True)
            assert service.provenance.get("stuck") == record
        finally:
            handle.release_close.set()
            if running is not None:
                await asyncio.gather(running, return_exceptions=True)
            await gateway.close()

    asyncio.run(exercise())


def test_buffered_completion_uses_the_existing_synchronous_settlement_path(tmp_path):
    async def exercise():
        source = _source("src_buffered01", "Buffered")
        service = _service(tmp_path, sources=[source], live_handles=[
            LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, status=200, source_id=source.id), (b'{"output":[]}',))
        ])
        model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service)
        runtime = ModelHubRuntimeRouter(service=service, turn_gateway=gateway, overlay_path=tmp_path / "overlay.json")
        request = _prepared_gateway_request(gateway, turn_id="buffered", requested_model=model, source_id=source.id, stream=False)
        try:
            response = await gateway._handle_request(request)
            assert response.status == 200
            assert runtime.settle_turn("buffered", settled_by=SETTLED_BY_TERMINAL_RESULT, ts="2026-09-06T00:00:00Z") is None
            assert service.provenance.get("buffered")["outcome"] == "served"
        finally:
            await gateway.close()

    asyncio.run(exercise())
