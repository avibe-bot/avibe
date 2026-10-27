"""Negative capability evidence learned from real upstream errors (capability model §3.2, §3.4).

The real engine client parses each upstream 400 through a loopback engine, so the
whole path is exercised: L1's closed rejection tokens, the narrow rule that names
exactly one (Source, model, feature), and the hop skip that the next request at a
refused effort gets instead of another provider 400.
"""

from __future__ import annotations

import json

import pytest
from aiohttp import web

from core.handlers.model_hub import service as service_module
from core.handlers.model_hub.capability_evidence import EvidenceFeature
from core.handlers.model_hub.request import ModelHubRequest
from core.handlers.model_hub.service import ModelHubError
from tests.test_model_hub_l3 import _assert_valid, _source
from tests.test_model_hub_retry_lifecycle import configured_service, loopback_engine
from vibe.backend_model_catalog import HostedToolSpec

ULTRA_REFUSED = {
    "error": {
        "code": "invalid_value",
        "type": "invalid_request_error",
        "message": "Invalid value: 'ultra'. Supported values are: 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', and 'max'.",
    }
}
TOOL_REFUSED = {"error": {"message": "Unsupported tool type: web_search", "type": "invalid_request_error"}}
# Measured on gemini-2.5-flash, 2026-10-27.
GOOGLE_SEARCH_REFUSED = {"error": {
    "code": 400, "status": "INVALID_ARGUMENT",
    "message": "Tool call context circulation is not enabled for models/gemini-2.5-flash",
}}
REPLAY_REFUSED = {
    "type": "error",
    "error": {"type": "invalid_request_error", "message": "Invalid encrypted_content in search_result block"},
}
WEB_SEARCH_HISTORY = [
    {"role": "user", "content": "搜索 Avibe"},
    {
        "role": "assistant",
        "content": [
            {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "Avibe"}},
            {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [{"encrypted_content": "Eo8B"}]},
        ],
    },
    {"role": "user", "content": "第二条结果是什么？"},
]

CASES = [
    # (case, backend, vendor, protocol, request body, upstream status, upstream body, learned feature, detail)
    (
        "effort-named-by-the-relay", "codex", "openai", "openai_responses",
        {"input": "hi", "reasoning": {"effort": "ultra"}}, 400, ULTRA_REFUSED,
        EvidenceFeature.effort("ultra"), "400 invalid_request_error",
    ),
    (
        "declared-tool-type", "codex", "openai", "openai_responses",
        {"input": "hi", "tools": [{"type": "web_search"}]}, 400, TOOL_REFUSED,
        EvidenceFeature.hosted("web_search", "call"), "400 invalid_request_error",
    ),
    (
        "replayed-search-result", "claude", "anthropic", "anthropic",
        {"max_tokens": 64, "messages": WEB_SEARCH_HISTORY}, 400, REPLAY_REFUSED,
        EvidenceFeature.hosted("web_search", "replay"), "400 invalid_request_error",
    ),
    ("another-effort-named", "codex", "openai", "openai_responses",
     {"input": "hi", "reasoning": {"effort": "high"}}, 400, ULTRA_REFUSED, None, None),
    ("same-value-in-another-field", "codex", "openai", "openai_responses",
     {"input": "hi", "reasoning": {"effort": "high"}}, 400,
     {"error": {"code": "invalid_value", "param": "service_tier", "message": "Invalid value: 'high'."}}, None, None),
    ("tool-type-not-declared", "codex", "openai", "openai_responses",
     {"input": "hi"}, 400, TOOL_REFUSED, None, None),
    ("no-hosted-result-replayed", "claude", "anthropic", "anthropic",
     {"max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}, 400, REPLAY_REFUSED, None, None),
    ("server-error-names-the-effort", "codex", "openai", "openai_responses",
     {"input": "hi", "reasoning": {"effort": "ultra"}}, 500, ULTRA_REFUSED, None, None),
    # Gemini refuses Google Search next to functions on a model that cannot circulate server-side calls.
    (
        "gemini-search-refused", "codex", "custom", "google",
        {"contents": [{"role": "user", "parts": [{"text": "hi"}]}], "tools": [{"googleSearch": {}}]}, 400,
        GOOGLE_SEARCH_REFUSED, EvidenceFeature.hosted("web_search", "call"), "400 invalid_request_error",
    ),
    ("gemini-search-not-declared", "codex", "custom", "google",
     {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}, 400, GOOGLE_SEARCH_REFUSED, None, None),
]


def _web_search_only(vendor, protocol, model_id, tool):
    if tool != "web_search":
        return None
    declaration = {
        "anthropic": {"type": "web_search_20250305", "name": "web_search"}, "google": {"type": "google_search"},
    }.get(protocol, {"type": "web_search"})
    return HostedToolSpec(declaration_json=json.dumps(declaration), price=None, include=())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend,vendor,protocol,body,status,upstream,feature,detail",
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
async def test_an_upstream_400_teaches_only_the_feature_it_names(
    tmp_path, monkeypatch, backend, vendor, protocol, body, status, upstream, feature, detail
):
    """MH-HOSTED-006: a narrow rule learns exactly the named feature; anything else learns nothing."""
    monkeypatch.setattr(service_module, "hosted_tool_spec", _web_search_only)
    source = _source("src_evidence1", "Relay 中转", vendor=vendor, protocol=protocol)
    if protocol == "google":
        source.base_url = "https://gemini.invalid"  # a Google Source names its root
    service, _clock, models = configured_service(tmp_path, sources=[source])

    async def respond(_request):
        return web.json_response(upstream, status=status)

    async with loopback_engine(tmp_path, service, respond):
        with pytest.raises(ModelHubError):
            await service.resolve_with_recovery(
                backend=backend, model_id=models[backend],
                request=ModelHubRequest(body, protocol=protocol), stream=False,
            )

    records = service.capability_evidence.records()
    if feature is None:
        assert records == []
        return
    [record] = records
    assert (record.source_id, record.model_id, record.feature) == ("src_evidence1", "shared-model", feature)
    assert (record.status, record.detail) == ("unsupported", detail)


@pytest.mark.parametrize(
    ("protocol", "model_id", "declared", "learned"),
    [
        # A custom relay runs OpenAI's declaration for an OpenAI model, so its refusal of it is learned (#2873).
        ("openai_responses", "gpt-6-luna", "web_search", EvidenceFeature.hosted("web_search", "call")),
        ("openai_responses", "grok-4.6", "x_search", EvidenceFeature.hosted("x_search", "call")),
        # A declaration the relay was never assumed to run teaches nothing about it.
        ("openai_responses", "deepseek-chat", "web_search", None),
    ],
    ids=["openai-model", "xai-model", "unclaimed-model"],
)
def test_a_custom_relays_refusal_of_a_declaration_it_was_assumed_to_run_is_learned(protocol, model_id, declared, learned):
    """MH-HOSTED-006: the narrow rule reads the same bundled priors that declared the tool, so a relay that refuses
    it is not sent the declaration again on every request."""
    from core.handlers.model_hub.adapter import UpstreamRejection
    from core.handlers.model_hub.evidence_rules import learn
    from vibe.backend_model_catalog import hosted_tool_spec

    relay = _source("src_relay0001", "Relay", vendor="custom", protocol=protocol, model_id=model_id)
    rejection = UpstreamRejection(status=400, error_type="invalid_request_error", unsupported_tool_type=declared)
    request = {"input": "hi", "tools": [{"type": declared}]}
    found = learn(
        request, rejection, request_protocol=protocol, source=relay, model_id=model_id, spec_for=hosted_tool_spec,
    )
    assert (found.feature if found is not None else None) == learned


@pytest.mark.asyncio
async def test_a_refused_effort_skips_its_hop_instead_of_another_provider_400(tmp_path):
    """MH-HOSTED-006: the next request at a refused effort fails locally, and the chain read shows it."""
    source = _source("src_evidence1", "Relay", vendor="openai", protocol="openai_responses")
    service, _clock, models = configured_service(tmp_path, sources=[source])
    calls = []

    async def respond(request):
        calls.append(await request.json())
        if calls[-1].get("reasoning", {}).get("effort") == "ultra":
            return web.json_response(ULTRA_REFUSED, status=400)
        return web.json_response(
            {"object": "response", "status": "completed", "output": [{"content": [{"type": "output_text", "text": "ok"}]}]}
        )

    def request(effort):
        return ModelHubRequest({"input": "hi", "reasoning": {"effort": effort}}, protocol="openai_responses")

    async with loopback_engine(tmp_path, service, respond):
        with pytest.raises(ModelHubError) as first:
            await service.resolve_with_recovery(
                backend="codex", model_id=models["codex"], request=request("ultra"), stream=False
            )
        assert first.value.code != "effort_unsupported"
        assert len(calls) == 1

        with pytest.raises(ModelHubError) as second:
            await service.resolve_with_recovery(
                backend="codex", model_id=models["codex"], request=request("ultra"), stream=False
            )
        assert (second.value.code, second.value.status) == ("effort_unsupported", 422)
        assert second.value.data == {"effort": "ultra", "source_ids": ["src_evidence1"]}
        assert len(calls) == 1

        # The chain read and the Source read project what routing just used.
        record = service.capability_evidence.records()[0]
        refusal = {
            "effort": "ultra",
            "basis": "observed",
            "observed_at": record.payload()["observed_at"],
            "detail": "400 invalid_request_error",
        }
        chain = service.agent_chain("codex", models["codex"])
        _assert_valid("agent-chain.schema.json", chain)
        [hop] = [hop for hop in chain["chain"] if hop["source_id"] == "src_evidence1"]
        assert hop["refused_efforts"] == [refusal]
        [listed] = [item for item in service.list_sources() if item["id"] == "src_evidence1"]
        assert listed["models"][0]["refused_efforts"] == [refusal]
        _assert_valid("source.schema.json", listed)

        served = await service.resolve_with_recovery(
            backend="codex", model_id=models["codex"], request=request("high"), stream=False
        )
        assert served.source_id == "src_evidence1"
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_refreshed_attempt_teaches_what_it_names(tmp_path):
    """MH-HOSTED-006: after a credential refresh, the exact retry's 400 is learned too."""
    from dataclasses import replace as replaced

    from core.handlers.model_hub.adapter import RawOutcomeKind, UpstreamRejection
    from tests.test_model_hub_l3 import _canonicalize_fixed_test_routes, _outcome, _service

    source = _source("src_refreshed1", "Refreshed")
    refused = replaced(
        _outcome(RawOutcomeKind.HTTP_ERROR, status=400, code="invalid_request_error", source_id=source.id),
        rejection=UpstreamRejection(status=400, error_type="invalid_request_error", invalid_value="ultra"),
    )
    service = _service(
        tmp_path,
        sources=[source],
        outcomes=[_outcome(RawOutcomeKind.HTTP_ERROR, status=401, code="unauthorized", source_id=source.id), refused],
    )
    service.adapter.refreshable_credential_refs.add(source.credential_ref)
    model = _canonicalize_fixed_test_routes(service)["codex"]

    with pytest.raises(ModelHubError):
        await service.resolve_with_recovery(
            backend="codex",
            model_id=model,
            request=ModelHubRequest({"input": "hi", "reasoning": {"effort": "ultra"}}, protocol="openai_responses"),
            stream=False,
        )

    [record] = service.capability_evidence.records()
    assert (record.source_id, record.feature) == ("src_refreshed1", EvidenceFeature.effort("ultra"))


@pytest.mark.asyncio
async def test_a_free_form_effort_is_forwarded_and_never_breaks_the_gateway(tmp_path):
    """MH-HOSTED-006: evidence cannot name a free-form effort, so it learns nothing and the 400 surfaces as is."""
    source = _source("src_evidence1", "Relay", vendor="openai", protocol="openai_responses")
    service, _clock, models = configured_service(tmp_path, sources=[source])
    refused = {"error": {"code": "invalid_value", "type": "invalid_request_error", "param": "reasoning.effort"}}

    async def respond(_request):
        return web.json_response(refused, status=400)

    async with loopback_engine(tmp_path, service, respond):
        for _ in range(2):
            with pytest.raises(ModelHubError) as error:
                await service.resolve_with_recovery(
                    backend="codex",
                    model_id=models["codex"],
                    request=ModelHubRequest(
                        {"input": "hi", "reasoning": {"effort": "future effort"}}, protocol="openai_responses"
                    ),
                    stream=False,
                )
            assert error.value.code != "effort_unsupported"
    assert service.capability_evidence.records() == []


@pytest.mark.asyncio
async def test_a_refused_effort_still_waits_for_a_capable_hop_that_is_cooling_down(tmp_path):
    """MH-HOSTED-006: effort_unsupported only when no hop that could recover accepts the effort."""
    from datetime import timedelta

    from core.handlers.model_hub.capability_evidence import EvidenceRecord, identity_digest
    from tests.test_model_hub_l3 import NOW, _canonicalize_fixed_test_routes, _service

    refusing = _source("src_refuses01", "Refuses ultra")
    cooling = _source(
        "src_cooling01", "Cooling", status="cooldown", retry_at=(NOW + timedelta(minutes=5)).isoformat()
    )
    service = _service(tmp_path, sources=[refusing, cooling])
    model = _canonicalize_fixed_test_routes(service)["codex"]
    service.capability_evidence.write_refusal(
        EvidenceRecord(
            source_id=refusing.id,
            model_id="shared-model",
            feature=EvidenceFeature.effort("ultra"),
            status="unsupported",
            observed_at=service.now(),
            wire=service.hosted_evidence().wire_for(refusing, "shared-model"),
            source_identity=identity_digest(refusing),
            detail="400 invalid_request_error",
        ),
        live=lambda: [refusing, cooling],
        wire_for=service.hosted_evidence().wire_for,
    )
    request = ModelHubRequest({"input": "hi", "reasoning": {"effort": "ultra"}}, protocol="openai_responses")

    with pytest.raises(ModelHubError) as waiting:
        await service.resolve(backend="codex", model_id=model, request=request)
    assert waiting.value.code != "effort_unsupported"
    assert waiting.value.supply_state == "waiting"
    assert service.adapter.invocations == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("vendor", "model_id"), [("openai", "shared-model"), ("custom", "gpt-6-luna")])
async def test_no_request_path_reads_a_bundled_file_after_startup(tmp_path, monkeypatch, vendor, model_id):
    """MH-HOSTED-006: every immutable bundled input is resolved when the service is built, never on a request.

    A custom Source's priors read its vendors' catalog protocols too (#2873)."""
    from dataclasses import replace as replaced
    from pathlib import Path

    from core.handlers.model_hub.adapter import RawOutcomeKind, UpstreamRejection
    from core.handlers.model_hub.stream_wire import ProtocolUsageReport
    from tests.test_model_hub_l3 import _canonicalize_fixed_test_routes, _outcome, _service
    from vibe.model_hub_runtime import api_key_vendors

    # Read once per process: cleared so that only the service's own startup can have read it.
    api_key_vendors.api_key_vendor_catalog.cache_clear()
    api_key_vendors._catalog_by_id.cache_clear()
    source = _source("src_startup01", "Startup", vendor=vendor, protocol="openai_responses", model_id=model_id)
    service = _service(tmp_path, sources=[source])
    model = _canonicalize_fixed_test_routes(service)["codex"]
    watched = {"backend_models.json", "cliproxyapi_manifest.json", "api_key_vendors.json"}
    reads = []
    read_text, path_open = Path.read_text, Path.open

    def counted_read(self, *args, **kwargs):
        if self.name in watched:
            reads.append(self.name)
        return read_text(self, *args, **kwargs)

    def counted_open(self, *args, **kwargs):
        if self.name in watched:
            reads.append(self.name)
        return path_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counted_read)
    monkeypatch.setattr(Path, "open", counted_open)

    request = ModelHubRequest(
        {"input": "hi", "reasoning": {"effort": "ultra"}, "tools": [{"type": "web_search"}]}, protocol="openai_responses"
    )
    rejected = replaced(
        _outcome(RawOutcomeKind.HTTP_ERROR, status=400, source_id=source.id),
        rejection=UpstreamRejection(status=400, unsupported_tool_type="web_search"),
    )
    config = service.store.load()
    assert service._effort_refused(source, model_id, request) is False
    await service._learn_capability_evidence(source, model_id, request, rejected)
    service.hosted_tools_for_hop(source, model_id, service.freeze_turn_hosted_tools(config))
    service.agent_chain("codex", model)
    service.list_sources()
    service.hosted_tools_status()
    service.hosted_call_cost(
        source,
        model_id,
        ("web_search",),
        usage=ProtocolUsageReport.of(input_tokens=10, cached_input_tokens=0, output_tokens=1),
        counts={"web_search_calls": 1},
    )
    assert reads == []


@pytest.mark.asyncio
async def test_a_refusing_runnable_hop_never_makes_recovery_spin(tmp_path):
    """MH-HOSTED-006: recovery waits for the capable cooling hop instead of re-resolving around the refusing one."""
    from datetime import timedelta as delta

    from core.handlers.model_hub.adapter import RawOutcomeKind
    from core.handlers.model_hub.capability_evidence import EvidenceRecord, identity_digest
    from tests.test_model_hub_l3 import NOW, _canonicalize_fixed_test_routes, _outcome
    from tests.test_model_hub_retry_policy import clock_service

    refusing = _source("src_refuses02", "Refuses ultra")
    cooling = _source("src_cooling02", "Cooling", status="cooldown", retry_at=(NOW + delta(seconds=30)).isoformat())
    service, clock = clock_service(
        tmp_path,
        sources=[refusing, cooling],
        outcomes=[_outcome(RawOutcomeKind.SUCCESS, status=200, source_id=cooling.id)],
    )
    model = _canonicalize_fixed_test_routes(service)["codex"]
    service.capability_evidence.write_refusal(
        EvidenceRecord(
            source_id=refusing.id,
            model_id="shared-model",
            feature=EvidenceFeature.effort("ultra"),
            status="unsupported",
            observed_at=service.now(),
            wire=service.hosted_evidence().wire_for(refusing, "shared-model"),
            source_identity=identity_digest(refusing),
            detail="400 invalid_request_error",
        ),
        live=lambda: [refusing, cooling],
        wire_for=service.hosted_evidence().wire_for,
    )
    passes = 0
    resolve = service.resolve

    async def counted(**kwargs):
        nonlocal passes
        passes += 1
        assert passes <= 5, "recovery re-resolved without waiting"
        return await resolve(**kwargs)

    service.resolve = counted
    served = await service.resolve_with_recovery(
        backend="codex",
        model_id=model,
        request=ModelHubRequest({"input": "hi", "reasoning": {"effort": "ultra"}}, protocol="openai_responses"),
    )

    assert served.source_id == cooling.id
    assert clock.delays and all(delay > 0 for delay in clock.delays)
    assert [invocation[0] for invocation in service.adapter.invocations] == [cooling.id]


MENU_CASES = [
    # (case, hops as (Source id, protocol, upstream model), refused (Source id, wire token) pairs,
    #  the menu row's efforts, the expected effort_menu)
    (
        "every-hop-or-some",
        [("src_menuhop01", "openai_responses", "gpt-6-sol"), ("src_menuhop02", "openai_responses", "gpt-6-sol")],
        [("src_menuhop01", "xhigh"), ("src_menuhop02", "xhigh"), ("src_menuhop01", "high")],
        ["low", "high", "xhigh"],
        {"omitted": ["xhigh"], "not_on": {"high": ["src_menuhop01"]}},
    ),
    # Vibey sends menu `minimal` to Anthropic as `low`, so a refused `low` refuses both menu values.
    (
        "menu-value-differs-from-wire",
        [("src_menuhop03", "anthropic", "claude-opus-4-7")],
        [("src_menuhop03", "low")],
        ["minimal", "low", "high"],
        {"omitted": ["minimal", "low"], "not_on": {}},
    ),
    # One request carries one token to every hop: the one written for the hop it is built on. Behind an OpenAI
    # primary, menu `minimal` reaches an Anthropic fallback as `minimal` too, so that is the evidence it is read under.
    (
        "the-primary-hop-decides-the-token",
        [("src_menuhop05", "openai_responses", "gpt-6-sol"), ("src_menuhop06", "anthropic", "claude-opus-4-7")],
        [("src_menuhop05", "minimal"), ("src_menuhop06", "minimal")],
        ["minimal", "low"],
        {"omitted": ["minimal"], "not_on": {}},
    ),
    (
        "nothing-refused",
        [("src_menuhop04", "openai_responses", "gpt-6-sol")],
        [],
        ["low", "high"],
        {"omitted": [], "not_on": {}},
    ),
]


@pytest.mark.parametrize("hops,refused,efforts,expected", [case[1:] for case in MENU_CASES], ids=[c[0] for c in MENU_CASES])
def test_the_picker_menu_states_which_efforts_routing_refuses_in_menu_values(tmp_path, hops, refused, efforts, expected):
    """MH-HOSTED-006: the Editor picker's read omits a tier every hop refuses and names the Sources of a tier only
    some refuse, in the values the menu offers, mapped to each hop's wire token by Vibey's own adapters."""
    from config.v2_config import ModelHubBackendModelConfig
    from core.handlers.model_hub.capability_evidence import EvidenceRecord, identity_digest
    from tests.test_model_hub_vibey_consumer import _vibey_service

    sources = [_source(source_id, source_id, vendor=protocol.split("_")[0], protocol=protocol, model_id=model)
               for source_id, protocol, model in hops]
    service = _vibey_service(tmp_path, sources)
    service.store.config.agents["vibey"].models = [ModelHubBackendModelConfig(id="menu-alias", reasoning_efforts=efforts)]
    live = service.store.load().sources
    by_id = {source.id: source for source in live}
    for source_id, token in refused:
        source = by_id[source_id]
        service.capability_evidence.write_refusal(
            EvidenceRecord(
                source_id=source_id,
                model_id=source.models[0].id,
                feature=EvidenceFeature.effort(token),
                status="unsupported",
                observed_at=service.now(),
                wire=service.hosted_evidence().wire_for(source, source.models[0].id),
                source_identity=identity_digest(source),
                detail="400 invalid_request_error",
            ),
            live=lambda: live,
            wire_for=service.hosted_evidence().wire_for,
        )

    agent = service.get_agent_sources("vibey")

    [row] = agent["catalog_models"]
    assert row["effort_menu"] == expected
    # The menu itself is unchanged: the editor saves it, and the read it submits back is accepted.
    assert row["reasoning_efforts"] == efforts
    assert list(ModelHubBackendModelConfig.from_payload(row).reasoning_efforts) == efforts
    _assert_valid("backend-model.schema.json", row)
