"""MH-HOSTED-GATEWAY-002/003: a hop receives hosted content only with current positive evidence.

A Vibey request carrying a native hosted declaration or a replayed native
payload is pinned (``x-avibe-pin-source``). The gateway serves it only from that
Source, unchanged, and answers ``pin_unavailable`` for every way it cannot be
served there before output, so Vibey can re-render it portably and resend it
unpinned (``vibey-hosted-tools.md`` §3.5). These cases drive the real gateway and
resolver against a recording engine adapter, which shows exactly which hop
received which body.
"""

from __future__ import annotations

import asyncio
import copy
import threading
from dataclasses import replace
from datetime import timedelta

import aiohttp
import pytest

from config.v2_config import ModelHubModelConfig, ModelHubRouteConfig, ModelHubRouteHopConfig
from core.handlers.model_hub import service as service_module
from core.handlers.model_hub.adapter import RawOutcomeKind, UpstreamRejection
from core.handlers.model_hub.capability_evidence import (
    CapabilityEvidenceStore, EvidenceFeature, EvidenceRecord, identity_digest,
)
from core.handlers.model_hub.hosted_admission import HostedContent, admits, hosted_content
from core.handlers.model_hub.capability_evidence import wire_stamp
from core.handlers.model_hub.hosted_tools import HostedEvidence
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from core.run_settlement import SETTLED_BY_STOPPED, SETTLED_BY_TERMINAL_RESULT
from modules.agents.model_hub import ModelHubRuntimeRouter
from tests.test_model_hub_l3 import NOW, InvokeHandle, LiveInvokeHandle, _outcome, _source
from tests.test_model_hub_vibey_consumer import _validate, _vibey_service
from vibe.backend_model_catalog import hosted_tool_spec

PIN = "x-avibe-pin-source"
PIN_MODEL = "x-avibe-pin-model"
PIN_UNAVAILABLE = {"type": "error", "error": {"type": "model_hub_pin_unavailable", "code": "pin_unavailable"}}


def _refused(tool, aspect, source_id):
    """The 409 a narrow hosted rule's handback answers: it names what was refused, and where."""

    reason = {"kind": "hosted_unsupported", "tool": tool, "aspect": aspect, "source_id": source_id}
    return {"type": "error", "error": {**PIN_UNAVAILABLE["error"], "reason": reason}}
SHELL = {"type": "function", "name": "shell", "parameters": {"type": "object", "properties": {}}}
PORTABLE_X_SEARCH = {"type": "function", "name": "x_search", "parameters": {"type": "object", "properties": {}}}
SIGNED_REASONING = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque-signed"}


def _sources(*, primary_status="standby", primary_model="grok-4.6"):
    retry_at = (NOW + timedelta(minutes=5)).isoformat() if primary_status == "cooldown" else None
    primary = _source("src_primary01", "xAI key", vendor="xai", model_id=primary_model,
                      status=primary_status, retry_at=retry_at)
    fallback = _source("src_fallback01", "OpenAI key", vendor="openai", model_id="gpt-6-astra")
    return primary, fallback


def _refuse(service, source, model_id, tool, aspect, *, engine=None):
    """A refusal a narrow rule wrote; ``engine`` names the release it was learned under (this one by default)."""

    service.capability_evidence.write_refusal(
        EvidenceRecord(
            source_id=source.id, model_id=model_id, feature=EvidenceFeature.hosted(tool, aspect),
            status="unsupported", observed_at=service.now(),
            wire=wire_stamp(
                engine=engine or service.engine_stamp, avibe=service.avibe_release, source=source, model_id=model_id,
            ),
            source_identity=identity_digest(source), detail="400 invalid_request_error",
        ),
        live=lambda: service.store.config.sources,
        # The wire this refusal was learned under, as the process that learned it rendered it.
        wire_for=lambda source, model_id: wire_stamp(
            engine=engine or service.engine_stamp, avibe=service.avibe_release, source=source, model_id=model_id,
        ),
    )


async def _post(hop, body, *, pin=None, model=None):
    headers = {"Authorization": f'Bearer {hop["token"]}', **hop["request_headers"]}
    if pin is not None:
        headers[PIN] = pin
    if model is not None:
        headers[PIN_MODEL] = model
    async with aiohttp.ClientSession() as client:
        async with client.post(f'{hop["base_url"]}/responses', headers=headers, json=body) as response:
            return response.status, await response.json(content_type=None)


def _native(hop, *, history=()):
    return {
        "model": hop["runtime_model"], "stream": False,
        "input": [*history, {"role": "user", "content": "posts from OpenAI this week"}],
        "tools": [{"type": "x_search"}, SHELL],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("case", "status", "invoked"), [
    ("served", 200, ["src_primary01"]),
    ("source_that_neither_launched_nor_served", 409, []),
    ("failure_before_output", 409, ["src_primary01"]),
    ("request_rejected", 400, ["src_primary01"]),
    # Nothing is probed: a tool the overlay offers is assumed until a real request refuses it.
    ("no_evidence_is_assumed", 200, ["src_primary01"]),
    ("current_refusal", 409, []),
    ("overlay_no_longer_offers_the_tool", 409, []),
    ("refusal_from_another_engine_no_longer_applies", 200, ["src_primary01"]),
    ("source_cooling_down", 409, []),
    ("source_not_on_route", 409, []),
    # Admission reads the snapshot the call is made on, after the engine is prepared.
    ("evidence_demoted_while_the_engine_is_prepared", 409, []),
])
async def test_a_pinned_request_is_served_only_by_its_source_or_handed_back(
    tmp_path, monkeypatch, case, status, invoked,
):
    """MH-HOSTED-GATEWAY-002: no failover, no recovery wait, no rewrite; refusals precede any invocation."""

    primary, fallback = _sources(
        primary_status="cooldown" if case == "source_cooling_down" else "standby",
        # The overlay offers x_search on grok-4 models only; evidence alone cannot admit it elsewhere.
        primary_model="grok-3-mini" if case == "overlay_no_longer_offers_the_tool" else "grok-4.6",
    )
    primary_model = primary.models[0].id
    outcome = {
        "failure_before_output": InvokeHandle(_outcome(RawOutcomeKind.HTTP_ERROR, status=429, code="rate_limit_error")),
        "request_rejected": InvokeHandle(_outcome(
            RawOutcomeKind.HTTP_ERROR, status=400, code="invalid_request_error", message="Bad field",
        )),
    }.get(case, LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=primary.id), (b"{}",)))
    service = _vibey_service(tmp_path, [primary, fallback], handles=[
        outcome, LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=fallback.id), (b"{}",)),
    ])
    service.recovery.window_seconds = 120
    if case == "current_refusal":
        _refuse(service, primary, primary_model, "x_search", "call")
    elif case == "refusal_from_another_engine_no_longer_applies":
        _refuse(service, primary, primary_model, "x_search", "call", engine="model-hub-engine-previous")
    if case == "evidence_demoted_while_the_engine_is_prepared":
        prepare = service._prepare_engine_for_demand

        async def demoting(*args, **kwargs):
            await prepare(*args, **kwargs)
            _refuse(service, primary, primary_model, "x_search", "call")

        service._prepare_engine_for_demand = demoting
    scanning_threads = []
    scan = service_module.hosted_content

    def recorded(*args, **kwargs):
        scanning_threads.append(threading.current_thread())
        return scan(*args, **kwargs)

    monkeypatch.setattr(service_module, "hosted_content", recorded)
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="vibey:pin", turn_id="turn-pin")
        # Vibey renders a pinned request for its pinned hop, so it carries the
        # hop's own signed history unchanged.
        body = _native(hop, history=[SIGNED_REASONING])
        pin = {"source_not_on_route": "src_absent0001", "source_that_neither_launched_nor_served": fallback.id}.get(
            case, primary.id,
        )
        if pin == fallback.id:
            body["tools"] = [{"type": "web_search"}, SHELL]
        sent = copy.deepcopy(body)
        answer_status, answer = await _post(hop, body, pin=pin)
    finally:
        await gateway.close()
    assert answer_status == status
    assert [entry[0] for entry in service.adapter.invocations] == invoked
    # Reading the request's whole history never runs on the controller loop.
    assert scanning_threads and threading.main_thread() not in scanning_threads
    if status == 409:
        assert answer == PIN_UNAVAILABLE
    if invoked:
        assert service.adapter.requests[0] == sent
    if case == "failure_before_output":
        # The failure still settles the Source's health, as normal routing would.
        assert service.store.config.sources[0].state.status == "cooldown"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "route_now_maps_the_source_to_another_model", "continuation_after_failover", "named_model_on_a_shared_source",
])
async def test_a_pin_names_the_exact_hop_it_was_rendered_for(tmp_path, case):
    """MH-HOSTED-GATEWAY-002: a pin is the (Source, model) the content was rendered for, never just the Source.

    The model is explicit (``x-avibe-pin-model``, the served-hop model a
    continuation continues) or the launch's immutable primary hop; never what
    served the turn last, since a turn can have concurrent requests. A pinned
    request is never stripped, so content for one model must not reach another.
    """

    primary, fallback = _sources()
    primary = replace(primary, models=[*primary.models, ModelHubModelConfig(id="grok-4.7", provenance="discovered")])
    service = _vibey_service(tmp_path, [primary, fallback], handles=[
        InvokeHandle(_outcome(RawOutcomeKind.HTTP_ERROR, status=429, code="rate_limit_error")),
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=fallback.id), (b"{}",)),
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=fallback.id), (b"{}",)),
    ] if case == "continuation_after_failover" else [
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=primary.id), (b"{}",)),
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=primary.id), (b"{}",)),
    ])
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="vibey:exact", turn_id="turn-exact")
        if case == "route_now_maps_the_source_to_another_model":
            # Mid-turn, the alias moves to another model on the pinned Source.
            service.store.config.agents["vibey"].routes["menu-alias"] = ModelHubRouteConfig((
                ModelHubRouteHopConfig(primary.id, "grok-4.7"), ModelHubRouteHopConfig(fallback.id, "gpt-6-astra"),
            ))
            assert await _post(hop, _native(hop), pin=primary.id) == (409, PIN_UNAVAILABLE)
            assert service.adapter.invocations == []
            return
        if case == "named_model_on_a_shared_source":
            service.store.config.agents["vibey"].routes["menu-alias"] = ModelHubRouteConfig((
                ModelHubRouteHopConfig(primary.id, "grok-4.6"), ModelHubRouteHopConfig(primary.id, "grok-4.7"),
            ))
            # A continuation of a response grok-4.7 served, then a new native request: each keeps its own hop,
            # whatever served last.
            assert (await _post(hop, _native(hop), pin=primary.id, model="grok-4.7"))[0] == 200
            assert (await _post(hop, _native(hop), pin=primary.id))[0] == 200
            assert [entry[:2] for entry in service.adapter.invocations] == [
                (primary.id, "grok-4.7"), (primary.id, "grok-4.6"),
            ]
            return
        # The previous response failed over to the fallback, which paused it; its continuation is pinned there.
        assert (await _post(hop, {**_native(hop), "tools": [SHELL]}))[0] == 200
        continuation = {**_native(hop, history=[SIGNED_REASONING, LOCAL_CALL, LOCAL_OUTPUT]), "tools": [SHELL]}
        # Without the served model, nothing says which hop it was rendered for.
        assert await _post(hop, continuation, pin=fallback.id) == (409, PIN_UNAVAILABLE)
        assert (await _post(hop, continuation, pin=fallback.id, model="gpt-6-astra"))[0] == 200
    finally:
        await gateway.close()
    assert [entry[:2] for entry in service.adapter.invocations] == [
        (primary.id, "grok-4.6"), (fallback.id, "gpt-6-astra"), (fallback.id, "gpt-6-astra"),
    ]
    assert service.adapter.requests[2] == continuation


@pytest.mark.asyncio
@pytest.mark.parametrize(("case", "statuses"), [
    ("same_identity", [200, 200]),
    ("credential_rotated_and_reverified", [200, 409]),
    ("no_pinned_response_this_turn", [409]),
])
async def test_a_continuation_stays_on_the_identity_its_response_was_served_under(tmp_path, case, statuses):
    """MH-HOSTED-GATEWAY-002: native content never crosses a Source's identity change within a turn.

    The turn's first admitted pinned request binds the Source's identity, and
    the in-flight response is exempt from replay evidence only on that bound
    identity. Evidence taken anew at a rotated credential does not carry the
    earlier response's payload across.
    """

    primary, fallback = _sources()
    service = _vibey_service(tmp_path, [primary, fallback], handles=[
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=primary.id), (b"{}",)) for _ in statuses
    ])
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    answers = []
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="vibey:identity", turn_id="turn-identity")
        if case != "no_pinned_response_this_turn":
            answers.append(await _post(hop, _native(hop), pin=primary.id))
        if case == "credential_rotated_and_reverified":
            live = service.store.config.sources[0]
            live.credential_ref = "cred_rotated0001"
        # Client tool results after the model's X search: the in-flight response carries its native item.
        continuation = {
            "model": hop["runtime_model"], "stream": False,
            "input": [{"role": "user", "content": "posts from OpenAI this week"}, X_CALL, LOCAL_CALL, LOCAL_OUTPUT],
            "tools": [{"type": "x_search"}, SHELL],
        }
        answers.append(await _post(hop, continuation, pin=primary.id))
    finally:
        await gateway.close()
    assert [status for status, _answer in answers] == statuses
    if statuses[-1] == 409:
        assert answers[-1][1] == PIN_UNAVAILABLE
    assert len(service.adapter.invocations) == statuses.count(200)


@pytest.mark.asyncio
async def test_a_pinned_request_rendered_for_the_launch_identity_is_handed_back_after_a_rotation(tmp_path):
    """MH-HOSTED-GATEWAY-002, C-6 item 6: a pinned request is never rewritten, so one rendered for the launch
    identity cannot go to its Source under another. A key rotated and re-verified before the turn's first pinned
    admission would otherwise bind the new identity while the body carries the old account's ciphertext, which the
    provider refuses as a request error; it is handed back, and Vibey renders it again portably."""

    primary, fallback = _sources()
    service = _vibey_service(tmp_path, [primary, fallback], handles=[
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=primary.id), (b"{}",)),
    ])
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="vibey:rotated", turn_id="turn-rotated")
        live = service.store.config.sources[0]
        live.credential_ref = "cred_rotated0001"
        answer = await _post(hop, _native(hop, history=[SIGNED_REASONING]), pin=primary.id)
    finally:
        await gateway.close()
    assert answer == (409, PIN_UNAVAILABLE)
    assert service.adapter.invocations == []


@pytest.mark.asyncio
async def test_overlapping_first_pinned_requests_bind_one_identity(tmp_path):
    """MH-HOSTED-GATEWAY-002: the identity is bound at final admission, so a concurrent first request cannot use another.

    The first request is admitted and still opening its call when the key is
    rotated and re-verified; the second, admitted on the new identity, is handed back.
    """

    primary, fallback = _sources()
    service = _vibey_service(tmp_path, [primary, fallback], handles=[
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=primary.id), (b"{}",)) for _ in range(2)
    ])
    admitted, release = asyncio.Event(), asyncio.Event()
    invoke = service.adapter.invoke

    async def held_first(*args, **kwargs):
        handle = await invoke(*args, **kwargs)
        if not admitted.is_set():
            admitted.set()
            await release.wait()
        return handle

    service.adapter.invoke = held_first
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="vibey:overlap", turn_id="turn-overlap")
        first = asyncio.create_task(_post(hop, _native(hop), pin=primary.id))
        await asyncio.wait_for(admitted.wait(), timeout=5)
        live = service.store.config.sources[0]
        live.credential_ref = "cred_rotated0001"
        assert await _post(hop, _native(hop), pin=primary.id) == (409, PIN_UNAVAILABLE)
        release.set()
        assert (await asyncio.wait_for(first, timeout=5))[0] == 200
    finally:
        release.set()
        await gateway.close()
    assert len(service.adapter.invocations) == 1


@pytest.mark.asyncio
async def test_the_portable_resend_fails_over_and_the_turn_settles_on_it(tmp_path):
    """MH-HOSTED-GATEWAY-002: the handed-back request leaves only its failed attempt in the turn."""

    primary, fallback = _sources()
    service = _vibey_service(tmp_path, [primary, fallback], handles=[
        InvokeHandle(_outcome(RawOutcomeKind.HTTP_ERROR, status=429, code="rate_limit_error")),
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=fallback.id), (b"{}",)),
    ])
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    turn_id = "turn-resend"
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="vibey:resend", turn_id=turn_id)
        assert await _post(hop, _native(hop), pin=primary.id) == (409, PIN_UNAVAILABLE)
        portable = {**_native(hop), "tools": [PORTABLE_X_SEARCH, SHELL]}
        assert (await _post(hop, portable))[0] == 200
        completion = router.settle_turn(turn_id, settled_by=SETTLED_BY_TERMINAL_RESULT, ts=NOW.isoformat())
        if completion:
            await completion
    finally:
        await gateway.close()
    assert [entry[0] for entry in service.adapter.invocations] == [primary.id, fallback.id]
    record = service.provenance.get(turn_id)
    _validate("turn-provenance.schema.json", record)
    assert record["outcome"] == "served"
    assert record["served"]["source_id"] == fallback.id
    assert [attempt["source_id"] for attempt in record["failed_attempts"]] == [primary.id]
    assert record["terminal_error"] is None


@pytest.mark.asyncio
async def test_an_ambiguous_pinned_failure_is_no_health_evidence_so_the_resend_may_try_it_again(tmp_path):
    """MH-HOSTED-GATEWAY-002: an unrecognized error frame in a 2xx writes no Source health, pinned or not.

    The handback carries no routing state: a turn's requests may overlap, so
    nothing turn-wide could tell the resend from another request.
    """

    primary, fallback = _sources()
    service = _vibey_service(tmp_path, [primary, fallback], handles=[
        InvokeHandle(_outcome(RawOutcomeKind.HTTP_ERROR, status=200, code="unrecognized_fixture")),
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=primary.id), (b"{}",)),
    ])
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="vibey:resend-walk", turn_id="turn-resend-walk")
        assert await _post(hop, _native(hop), pin=primary.id) == (409, PIN_UNAVAILABLE)
        assert (await _post(hop, {**_native(hop), "tools": [PORTABLE_X_SEARCH, SHELL]}))[0] == 200
    finally:
        await gateway.close()
    assert [entry[0] for entry in service.adapter.invocations] == [primary.id, primary.id]
    assert service.store.config.sources[0].state.status == "standby"


@pytest.mark.asyncio
@pytest.mark.parametrize(("case", "tools", "history", "status", "invoked"), [
    # Hosted content is Source-specific, so it travels only pinned; unpinned, no hop is right for it.
    ("native_declaration", [{"type": "web_search"}], [], 422, []),
    ("replayed_hosted_item", [SHELL], ["WEB_CALL"], 422, []),
    # Function tools and plain history are never hosted content.
    ("portable_request", [PORTABLE_X_SEARCH], [], 200, ["src_primary01"]),
])
async def test_unpinned_hosted_content_is_refused_before_anything_runs(tmp_path, case, tools, history, status, invoked):
    """MH-HOSTED-GATEWAY-002: the gateway never chooses a hop for hosted content; Vibey's pin does."""

    primary, fallback = _sources()
    service = _vibey_service(tmp_path, [primary, fallback], handles=[
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=primary.id), (b"{}",)),
    ])
    # Evidence everywhere does not make unpinned content admissible.
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="vibey:unpinned", turn_id="turn-unpinned")
        body = {**_native(hop, history=[WEB_CALL, ASSISTANT_TEXT] if history else []), "tools": [*tools, SHELL]}
        answer_status, answer = await _post(hop, body)
    finally:
        await gateway.close()
    assert answer_status == status
    assert [entry[0] for entry in service.adapter.invocations] == invoked
    if status == 422:
        assert answer["error"]["code"] == "hosted_content_unpinned"


@pytest.mark.asyncio
async def test_a_handed_back_refusal_does_not_end_the_turn(tmp_path):
    """MH-HOSTED-GATEWAY-002: the handback withdraws its own surfaced terminal; the resend's outcome decides the turn."""

    primary, _fallback = _sources()
    refusal = replace(
        _outcome(RawOutcomeKind.HTTP_ERROR, status=400, code="invalid_request_error"),
        rejection=UpstreamRejection(status=400, code="invalid_request_error", unsupported_tool_type="x_search"),
    )
    service = _vibey_service(tmp_path, [primary], handles=[InvokeHandle(refusal)])
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    turn_id = "turn-withdraw"
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="vibey:withdraw", turn_id=turn_id)
        assert await _post(hop, _native(hop), pin=primary.id) == (409, _refused("x_search", "call", primary.id))
        # The turn ends before any resend (the user stops it): the handed-back
        # refusal must not stand as the turn's terminal error.
        completion = router.settle_turn(turn_id, settled_by=SETTLED_BY_STOPPED, ts=NOW.isoformat())
        if completion:
            await completion
    finally:
        await gateway.close()
    record = service.provenance.get(turn_id)
    _validate("turn-provenance.schema.json", record)
    # Not the handed-back hosted-tool refusal: that request never ended the turn.
    assert record["terminal_error"] is None
    assert record["outcome"] != "failed_terminal"


@pytest.mark.asyncio
async def test_a_pinned_hop_must_speak_the_requests_protocol(tmp_path):
    """MH-HOSTED-GATEWAY-002: native content is never translated; a pin across protocols is handed back."""

    primary, fallback = _sources()
    service = _vibey_service(tmp_path, [primary, fallback])
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="vibey:protocol", turn_id="turn-protocol")
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f'{hop["base_url"]}/messages',
                headers={"Authorization": f'Bearer {hop["token"]}', **hop["request_headers"], PIN: primary.id},
                json={"model": hop["runtime_model"], "max_tokens": 64, "messages": [{"role": "user", "content": "q"}]},
            ) as response:
                assert (response.status, await response.json()) == (409, PIN_UNAVAILABLE)
    finally:
        await gateway.close()
    assert service.adapter.invocations == []


@pytest.mark.asyncio
async def test_a_refusal_naming_the_pinned_hosted_content_is_handed_back_and_learned(tmp_path):
    """MH-HOSTED-GATEWAY-002: case 3, a narrow hosted-content rule (#2551) turns the pinned 400 into a handback with its reason."""

    primary, fallback = _sources()
    refusal = replace(
        _outcome(RawOutcomeKind.HTTP_ERROR, status=400, code="invalid_request_error",
                 message="Unsupported tool type: x_search"),
        rejection=UpstreamRejection(status=400, code="invalid_request_error", error_type="invalid_request_error",
                                    unsupported_tool_type="x_search"),
    )
    service = _vibey_service(tmp_path, [primary, fallback], handles=[InvokeHandle(refusal)])
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="vibey:learn", turn_id="turn-learn")
        # The 409 names the refused feature; only a narrow rule's handback carries a reason.
        assert await _post(hop, _native(hop), pin=primary.id) == (409, _refused("x_search", "call", primary.id))
    finally:
        await gateway.close()
    assert [entry[0] for entry in service.adapter.invocations] == [primary.id]
    learned = service.hosted_evidence().refusal(primary, "grok-4.6", EvidenceFeature.hosted("x_search", "call"))
    assert learned is not None and learned.detail.startswith("400")


# Responses and Anthropic histories: (case, protocol, history, pinned, the tools needing replay evidence).
WEB_CALL = {"type": "web_search_call", "id": "ws_1", "status": "completed", "action": {"type": "search", "query": "q"}}
X_CALL = {"type": "custom_tool_call", "name": "x_keyword_search", "call_id": "xs_1", "input": "{}"}
IMAGE_CALL = {"type": "image_generation_call", "id": "ig_1", "status": "completed", "result": "path record"}
LOCAL_CALL = {"type": "function_call", "call_id": "fc_1", "name": "shell", "arguments": "{}"}
LOCAL_OUTPUT = {"type": "function_call_output", "call_id": "fc_1", "output": "ok"}
USER = {"role": "user", "content": "next"}
ASSISTANT_TEXT = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "answer"}]}
SERVER_USE = {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "q"}}
SERVER_RESULT = {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": []}
TOOL_USE = {"type": "tool_use", "id": "toolu_1", "name": "shell", "input": {}}


def _tool_result(call_id):
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id, "content": "ok"}]}


GEMINI_USER = {"role": "user", "parts": [{"text": "search"}]}
GEMINI_SEARCH_CALL = {"toolCall": {"toolType": "GOOGLE_SEARCH_WEB", "args": {"queries": ["q"]}, "id": "s1"},
                      "thoughtSignature": "sig"}
GEMINI_SEARCH_RESULT = {"toolResponse": {"toolType": "GOOGLE_SEARCH_WEB", "response": {}, "id": "s1"},
                        "thoughtSignature": "sig"}
GEMINI_CALL = {"functionCall": {"name": "shell", "args": {}, "id": "f1"}}
GEMINI_RESULT = {"role": "user", "parts": [{"functionResponse": {"name": "shell", "id": "f1", "response": {}}}]}

IN_FLIGHT_CASES = [
    ("responses_cross_turn", "openai_responses", [USER, WEB_CALL, ASSISTANT_TEXT, USER], True, {"web_search"}),
    ("responses_in_flight", "openai_responses", [USER, WEB_CALL, X_CALL, LOCAL_CALL, LOCAL_OUTPUT], True, set()),
    ("responses_in_flight_unpinned", "openai_responses", [USER, WEB_CALL, LOCAL_CALL, LOCAL_OUTPUT], False,
     {"web_search"}),
    # An earlier model call of the same turn is history, not the in-flight response.
    ("responses_earlier_step_of_turn", "openai_responses",
     [USER, WEB_CALL, LOCAL_CALL, LOCAL_OUTPUT, LOCAL_CALL, LOCAL_OUTPUT], True, {"web_search"}),
    ("responses_steer_after_tools", "openai_responses", [USER, X_CALL, LOCAL_CALL, LOCAL_OUTPUT, USER], True,
     {"x_search"}),
    ("responses_image_cross_turn", "openai_responses", [USER, IMAGE_CALL, ASSISTANT_TEXT, USER], True,
     {"image_generation"}),
    # An image call is never replayed verbatim, even in flight: its result is Vibey's path record.
    ("responses_image_in_flight", "openai_responses", [USER, IMAGE_CALL, LOCAL_CALL, LOCAL_OUTPUT], True,
     {"image_generation"}),
    # Only known model output forms the in-flight response; an unknown item ends it.
    ("responses_unknown_client_item_after_hosted_call", "openai_responses",
     [USER, WEB_CALL, {"type": "computer_call_output", "call_id": "cc_1", "output": {}}], True, {"web_search"}),
    ("responses_unknown_custom_call", "openai_responses",
     [USER, {**X_CALL, "name": "local_freeform"}, ASSISTANT_TEXT, USER], True, set()),
    ("anthropic_pause_turn", "anthropic",
     [USER, {"role": "assistant", "content": [SERVER_USE, SERVER_RESULT, SERVER_USE]}], True, set()),
    ("anthropic_parallel_tool_results", "anthropic",
     [USER, {"role": "assistant", "content": [SERVER_USE, SERVER_RESULT, TOOL_USE, {**TOOL_USE, "id": "toolu_2"}]},
      _tool_result("toolu_1"), _tool_result("toolu_2")], True, set()),
    ("anthropic_cross_turn", "anthropic",
     [USER, {"role": "assistant", "content": [SERVER_USE, SERVER_RESULT]}, USER], True, {"web_search"}),
    ("anthropic_earlier_message", "anthropic",
     [USER, {"role": "assistant", "content": [SERVER_RESULT, TOOL_USE]}, _tool_result("toolu_1"),
      {"role": "assistant", "content": [{**TOOL_USE, "id": "toolu_2"}]}, _tool_result("toolu_2")], True,
     {"web_search"}),
    ("anthropic_non_object_entry", "anthropic",
     [USER, {"role": "assistant", "content": [SERVER_USE, SERVER_RESULT]}, "not a message"], True, {"web_search"}),
    ("anthropic_unpinned", "anthropic",
     [USER, {"role": "assistant", "content": [SERVER_USE, SERVER_RESULT]}], False, {"web_search"}),
    # A field that is not text is absent: an item with one is no hosted item and ends the in-flight run.
    # A named call without a textual name is malformed, so it ends the in-flight run.
    ("responses_custom_call_name_not_text", "openai_responses",
     [USER, WEB_CALL, {**X_CALL, "name": {"x": 1}}, LOCAL_CALL, LOCAL_OUTPUT], True, {"web_search"}),
    ("responses_custom_call_without_name", "openai_responses",
     [USER, WEB_CALL, {key: value for key, value in X_CALL.items() if key != "name"}, LOCAL_CALL, LOCAL_OUTPUT],
     True, {"web_search"}),
    ("responses_custom_call_blank_name", "openai_responses",
     [USER, WEB_CALL, {**X_CALL, "name": " "}, LOCAL_CALL, LOCAL_OUTPUT], True, {"web_search"}),
    ("responses_function_call_without_name", "openai_responses",
     [USER, WEB_CALL, {key: value for key, value in LOCAL_CALL.items() if key != "name"}, LOCAL_OUTPUT], True,
     {"web_search"}),
    ("responses_item_type_not_text", "openai_responses",
     [USER, WEB_CALL, LOCAL_CALL, {"type": ["function_call_output"], "call_id": "fc_1"}], True, {"web_search"}),
    ("anthropic_server_tool_name_not_text", "anthropic",
     [USER, {"role": "assistant", "content": [{**SERVER_USE, "name": ["web_search"]}]}, USER], True, set()),
    # Gemini: the in-flight response is the last model content, followed only by function responses.
    ("google_in_flight", "google",
     [GEMINI_USER, {"role": "model", "parts": [GEMINI_SEARCH_CALL, GEMINI_SEARCH_RESULT, GEMINI_CALL]},
      GEMINI_RESULT], True, set()),
    ("google_cross_turn", "google",
     [GEMINI_USER, {"role": "model", "parts": [GEMINI_SEARCH_CALL, GEMINI_SEARCH_RESULT]}, GEMINI_USER], True,
     {"web_search"}),
    ("google_unpinned", "google",
     [GEMINI_USER, {"role": "model", "parts": [GEMINI_SEARCH_CALL, GEMINI_SEARCH_RESULT, GEMINI_CALL]},
      GEMINI_RESULT], False, {"web_search"}),
    # A server-side tool Gemini ran for anything but a search is not a hosted call of the catalog's.
    ("google_other_server_tool", "google",
     [GEMINI_USER, {"role": "model", "parts": [{"toolCall": {"toolType": "URL_CONTEXT", "id": "u1"}}]}, GEMINI_USER],
     True, set()),
]


@pytest.mark.parametrize(("protocol", "history", "pinned", "replays"), [case[1:] for case in IN_FLIGHT_CASES],
                         ids=[case[0] for case in IN_FLIGHT_CASES])
def test_only_items_outside_the_in_flight_response_need_replay_evidence(protocol, history, pinned, replays):
    """MH-HOSTED-GATEWAY-003: the structural in-flight rule agreed with the Vibey request builder."""

    field = {"openai_responses": "input", "google": "contents"}.get(protocol, "messages")
    content = hosted_content({field: history, "tools": [SHELL]}, protocol, pinned=pinned)
    assert content.replays == frozenset(replays)
    assert content.calls == frozenset()


@pytest.mark.parametrize(("case", "kind", "channel", "listed", "admitted"), [
    ("api_key_listed", "api_key", "hub", "listed", True),
    ("api_key_unlisted", "api_key", "hub", "unlisted", True),
    ("api_key_retired_tombstone", "api_key", "hub", "retired", False),
    ("subscription_unlisted", "subscription", "hub", "unlisted", False),
    ("native_channel", "api_key", "native_cli", "listed", False),
])
def test_admission_runs_a_tool_only_where_hop_resolution_would(tmp_path, case, kind, channel, listed, admitted):
    """MH-HOSTED-GATEWAY-002: admission uses ``runs_on``, so old evidence never admits a hop the tool cannot run on."""

    model_id = "gpt-6-astra"
    source = replace(_source("src_openai0001", "OpenAI", vendor="openai", model_id=model_id), kind=kind,
                     supply_channel=channel)
    if listed != "listed":
        source.models = [ModelHubModelConfig(id=model_id if listed == "retired" else "gpt-6-other",
                                             provenance="discovered", retired=listed == "retired" or None)]
    evidence = HostedEvidence(CapabilityEvidenceStore(tmp_path / "evidence.json"), "engine", "avibe")
    content = hosted_content({"tools": [{"type": "web_search"}]}, "openai_responses", pinned=True)
    assert admits(content, source=source, model_id=model_id, spec_for=hosted_tool_spec, evidence=evidence) is admitted


@pytest.mark.parametrize(("refused", "admitted"), [
    ((), True),
    (("replay",), False),
    # A call refusal withdraws replay too, as the hop's facts state; a concurrent refusal must not be outrun.
    (("call",), False),
])
def test_a_replay_is_admitted_exactly_where_the_hops_facts_assume_it(tmp_path, refused, admitted):
    """MH-HOSTED-GATEWAY-002: admission and hop resolution read one rule, so they never disagree about a replay."""

    source = _source("src_xai000001", "xAI", vendor="xai", model_id="grok-4.6")
    store = CapabilityEvidenceStore(tmp_path / "evidence.json")
    evidence = HostedEvidence(store, "engine", "avibe")
    for aspect in refused:
        store.write_refusal(
            EvidenceRecord(
                source_id=source.id, model_id="grok-4.6", feature=EvidenceFeature.hosted("web_search", aspect),
                status="unsupported", observed_at=NOW, wire=evidence.wire_for(source, "grok-4.6"),
                source_identity=identity_digest(source), detail="400 invalid_request_error",
            ),
            live=lambda: [source],
            wire_for=evidence.wire_for,
        )
    content = HostedContent(replays=frozenset({"web_search"}))
    assert admits(content, source=source, model_id="grok-4.6", spec_for=hosted_tool_spec, evidence=evidence) is admitted


def test_declarations_are_recognized_by_every_vendors_type():
    """MH-HOSTED-GATEWAY-003: a declaration counts whichever vendor offers it, never a function."""

    # No chat request declares a hosted image tool any more, so its type is no declaration.
    tools = [{"type": "x_search"}, {"type": "web_search"}, {"type": "image_generation", "partial_images": 2},
             PORTABLE_X_SEARCH, {"type": "web_search_20250305", "name": "web_search"}]
    assert hosted_content({"tools": tools}, "openai_responses", pinned=True).calls == frozenset(
        {("x_search", "x_search"), ("web_search", "web_search")}
    )
    assert hosted_content({"tools": tools}, "anthropic", pinned=True).calls == frozenset(
        {("web_search", "web_search_20250305")}
    )
    assert not hosted_content({"tools": tools}, "openai_chat", pinned=True)
    # Gemini declares Google Search as a keyed tool object, named by the overlay's type.
    gemini_tools = [{"functionDeclarations": [{"name": "web_search"}]}, {"googleSearch": {}}]
    assert hosted_content({"tools": gemini_tools}, "google", pinned=True).calls == frozenset(
        {("web_search", "google_search")}
    )
