"""Hosted-tool hop resolution (``vibey-hosted-tools.md`` §3.4, §3.5; §12 items 7, 14, 19, 20).

Native is reachable only through positive evidence on the current wire, an
eligible credential and an untranslated path; everything else is the proxy.
The executor is bound for every enabled tool, whatever the mode.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from config.v2_config import (
    ModelHubAgentSupplyConfig,
    ModelHubConfig,
    ModelHubHostedToolConfig,
    ModelHubModelConfig,
    ModelHubRouteHopConfig,
    ModelHubSourceConfig,
    ModelHubSourceStateConfig,
)
from core.handlers.model_hub.capability_evidence import (
    CapabilityEvidenceStore,
    EvidenceFeature,
    EvidenceRecord,
    identity_digest,
)
from core.handlers.model_hub.hosted_tools import hosted_tools_status, resolve_hosted_tools
from vibe.backend_model_catalog import HostedToolSpec

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
ENGINE = "model-hub-engine-v8.0.23-3"


def _spec(declaration: dict, include: tuple[str, ...] = ()) -> HostedToolSpec:
    return HostedToolSpec(declaration_json=json.dumps(declaration), price=None, include=include)


SPECS = {
    ("xai", "openai_responses", "web_search"): _spec({"type": "web_search"}),
    ("xai", "openai_responses", "x_search"): _spec({"type": "x_search"}),
    ("openai", "openai_responses", "web_search"): _spec({"type": "web_search"}, ("web_search_call.action.sources",)),
    ("openai", "openai_responses", "image_generation"): _spec({"type": "image_generation", "partial_images": 2}),
    ("anthropic", "anthropic", "web_search"): _spec({"type": "web_search_20250305", "name": "web_search"}),
    ("google", "google", "web_search"): _spec({"type": "google_search"}),
}


def _spec_for(vendor, protocol, model_id, tool):
    return SPECS.get((vendor, protocol, tool))


def _source(source_id, vendor, protocol, model_id, *, kind="api_key") -> ModelHubSourceConfig:
    return ModelHubSourceConfig(
        id=source_id,
        kind=kind,
        vendor=vendor,
        display_name=source_id,
        protocol=protocol,
        supply_channel="hub",
        billing="monthly" if kind == "subscription" else "metered",
        state=ModelHubSourceStateConfig(status="standby"),
        models=[ModelHubModelConfig(id=model_id, provenance="discovered")],
        credential_ref=f"cred_{source_id}",
    )


XAI = _source("src_xai00001", "xai", "openai_responses", "grok-4.6")
OPENAI = _source("src_oai00001", "openai", "openai_responses", "gpt-6-astra")
CLAUDE_SUB = _source("src_cla00001", "anthropic", "anthropic", "claude-opus-5-5", kind="subscription")
GEMINI = _source("src_gem00001", "google", "google", "gemini-3-pro")
ALL = [CLAUDE_SUB, XAI, OPENAI, GEMINI]


def _config(tools: dict[str, ModelHubHostedToolConfig]) -> ModelHubConfig:
    vibey = ModelHubAgentSupplyConfig.default("vibey", mode="hub")
    vibey = replace(vibey, sources=replace(vibey.sources, order=[source.id for source in ALL]))
    config = ModelHubConfig(sources=list(ALL), hosted_tools=tools)
    config.agents["vibey"] = vibey
    return config


def _store(tmp_path, *verified, engine=ENGINE) -> CapabilityEvidenceStore:
    store = CapabilityEvidenceStore(tmp_path / "evidence.json")
    for source, tool, aspect in verified:
        store.write(
            EvidenceRecord(
                source_id=source.id,
                model_id=source.models[0].id,
                feature=EvidenceFeature.hosted(tool, aspect),
                status="verified",
                basis="probe",
                observed_at=NOW,
                engine=engine,
                shape=1,
                source_identity=identity_digest(source),
            ),
            live=ALL,
        )
    return store


def _resolve(tmp_path, tools, primary, *verified, engine=ENGINE, evidence_engine=ENGINE):
    return resolve_hosted_tools(
        _config(tools),
        source=primary,
        model_id=primary.models[0].id,
        evidence=_store(tmp_path, *verified, engine=evidence_engine),
        spec_for=_spec_for,
        engine=engine,
        now=NOW,
    )


ON = ModelHubHostedToolConfig(enabled=True)


def test_only_enabled_tools_resolve(tmp_path):
    assert _resolve(tmp_path, {}, XAI) == {}
    assert _resolve(tmp_path, {"web_search": ModelHubHostedToolConfig(enabled=False)}, XAI) == {}


def test_no_evidence_means_proxy_without_an_executor(tmp_path):
    assert _resolve(tmp_path, {"web_search": ON, "image_generation": ON}, XAI) == {
        "web_search": {"mode": "proxy", "executor": None, "replay": False},
        "image_generation": {"mode": "proxy", "executor": None, "replay": False},
    }


def test_verified_call_makes_the_hop_native_and_binds_the_first_verified_executor(tmp_path):
    resolved = _resolve(
        tmp_path,
        {"web_search": ON, "x_search": ON},
        OPENAI,
        (OPENAI, "web_search", "call"),
        (OPENAI, "web_search", "replay"),
        (XAI, "x_search", "call"),
    )
    assert resolved == {
        "web_search": {
            "mode": "native",
            "declaration": {"type": "web_search"},
            "include": ["web_search_call.action.sources"],
            "executor": {"source_id": "src_oai00001", "model_id": "gpt-6-astra"},
            "replay": True,
        },
        "x_search": {
            "mode": "proxy",
            "executor": {"source_id": "src_xai00001", "model_id": "grok-4.6"},
            "replay": False,
        },
    }


def test_evidence_from_another_engine_routes_as_proxy(tmp_path):
    resolved = _resolve(
        tmp_path,
        {"web_search": ON},
        XAI,
        (XAI, "web_search", "call"),
        (XAI, "web_search", "replay"),
        engine="model-hub-engine-v8.0.24-1",
    )
    assert resolved == {"web_search": {"mode": "proxy", "executor": None, "replay": False}}


@pytest.mark.parametrize(
    "primary",
    [CLAUDE_SUB, GEMINI],
    ids=["claude-subscription-is-never-eligible", "google-is-a-translated-path"],
)
def test_ineligible_or_translated_hops_never_go_native_or_execute(tmp_path, primary):
    resolved = _resolve(
        tmp_path, {"web_search": ON}, primary, (primary, "web_search", "call"), (primary, "web_search", "replay")
    )
    assert resolved == {"web_search": {"mode": "proxy", "executor": None, "replay": False}}


def test_the_chosen_executor_wins_only_while_verified(tmp_path):
    chosen = ModelHubHostedToolConfig(enabled=True, executor=ModelHubRouteHopConfig("src_xai00001", "grok-4.6"))
    verified_both = (
        (OPENAI, "web_search", "call"),
        (XAI, "web_search", "call"),
    )
    assert _resolve(tmp_path / "a", {"web_search": chosen}, GEMINI, *verified_both)["web_search"]["executor"] == {
        "source_id": "src_xai00001",
        "model_id": "grok-4.6",
    }
    assert _resolve(tmp_path / "b", {"web_search": chosen}, GEMINI, verified_both[0])["web_search"]["executor"] == {
        "source_id": "src_oai00001",
        "model_id": "gpt-6-astra",
    }


def test_image_generation_is_never_replayed(tmp_path):
    resolved = _resolve(tmp_path, {"image_generation": ON}, OPENAI, (OPENAI, "image_generation", "call"))
    assert resolved["image_generation"] == {
        "mode": "native",
        "declaration": {"type": "image_generation", "partial_images": 2},
        "include": [],
        "executor": {"source_id": "src_oai00001", "model_id": "gpt-6-astra"},
        "replay": False,
    }


def test_status_lists_where_each_tool_can_run_and_counts_re_verifications(tmp_path):
    store = _store(tmp_path, (XAI, "x_search", "call"), (OPENAI, "web_search", "call"), engine="model-hub-engine-v8.0.22-1")
    store.write(
        EvidenceRecord(
            source_id=XAI.id,
            model_id="grok-4.6",
            feature=EvidenceFeature.hosted("web_search", "call"),
            status="verified",
            basis="probe",
            observed_at=NOW.replace(month=8),
            engine=ENGINE,
            shape=1,
            source_identity=identity_digest(XAI),
        ),
        live=ALL,
    )
    status = hosted_tools_status(
        _config({"x_search": ON}), evidence=store, spec_for=_spec_for, engine=ENGINE, now=NOW
    )
    by_name = {tool["name"]: tool for tool in status["tools"]}
    assert [tool["name"] for tool in status["tools"]] == ["web_search", "x_search", "image_generation"]
    assert by_name["x_search"]["enabled"] is True and by_name["web_search"]["enabled"] is False
    assert [(c["source_id"], c["call"]["status"], c["call"]["stale"]) for c in by_name["x_search"]["candidates"]] == [
        ("src_xai00001", "unknown", "wire")
    ]
    assert [(c["source_id"], c["call"]["status"], c["call"]["stale"]) for c in by_name["web_search"]["candidates"]] == [
        ("src_xai00001", "verified", "age"),
        ("src_oai00001", "unknown", "wire"),
    ]
    assert by_name["image_generation"]["candidates"][0]["replay"] is None
    assert status["reverify"] == {"engine_upgraded": 2, "aged": 1}
