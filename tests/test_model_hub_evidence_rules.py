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
    ("tool-type-not-declared", "codex", "openai", "openai_responses",
     {"input": "hi"}, 400, TOOL_REFUSED, None, None),
    ("no-hosted-result-replayed", "claude", "anthropic", "anthropic",
     {"max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}, 400, REPLAY_REFUSED, None, None),
    ("server-error-names-the-effort", "codex", "openai", "openai_responses",
     {"input": "hi", "reasoning": {"effort": "ultra"}}, 500, ULTRA_REFUSED, None, None),
]


def _web_search_only(vendor, protocol, model_id, tool):
    if tool != "web_search":
        return None
    declaration = {"anthropic": {"type": "web_search_20250305", "name": "web_search"}}.get(
        protocol, {"type": "web_search"}
    )
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
    assert (record.status, record.basis, record.detail) == ("unsupported", "observed", detail)


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

        # The chain read projects what routing just used, for the effort menu.
        chain = service.agent_chain("codex", models["codex"])
        _assert_valid("agent-chain.schema.json", chain)
        [hop] = [hop for hop in chain["chain"] if hop["source_id"] == "src_evidence1"]
        assert hop["refused_efforts"] == [
            {
                "effort": "ultra",
                "basis": "observed",
                "observed_at": service.capability_evidence.records()[0].payload()["observed_at"],
                "detail": "400 invalid_request_error",
            }
        ]

        served = await service.resolve_with_recovery(
            backend="codex", model_id=models["codex"], request=request("high"), stream=False
        )
        assert served.source_id == "src_evidence1"
        assert len(calls) == 2
