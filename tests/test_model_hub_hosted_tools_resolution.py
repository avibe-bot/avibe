"""Hosted-tool facts, the executor chain, the image enum and the Tools read (``vibey-hosted-tools.md`` §3.3–§3.4, §6,
§7.2).

Nothing is probed: a tool the overlay offers is assumed on a hop until a real
request refuses it, and these functions are the one place that decides so.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from config.v2_config import (
    ModelHubAgentSupplyConfig,
    ModelHubConfig,
    ModelHubHostedToolConfig,
    ModelHubImageModelChoice,
    ModelHubModelConfig,
    ModelHubSourceConfig,
    ModelHubSourceStateConfig,
)
from core.handlers.model_hub import hosted_tools as hosted_tools_module
from core.handlers.model_hub.capability_evidence import (
    CapabilityEvidenceStore,
    EvidenceFeature,
    EvidenceRecord,
    identity_digest,
)
from core.handlers.model_hub.hosted_tools import (
    HostedEvidence,
    HostedToolSettingRefused,
    apply_hosted_tool_put,
    executor_candidates,
    hosted_tool_facts,
    hosted_tools_status,
    image_choices,
    resolve_hosted_tools,
)
from vibe.backend_model_catalog import HostedToolPrice, HostedToolSpec, ImageModelPrice, ImageModelSpec

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
ENGINE, AVIBE = "model-hub-engine-v8.0.23-3", "3.2.1"
SEARCH_PRICE = HostedToolPrice(unit="call", usd_per_1k=5, as_of=date(2026, 10, 13), url="https://docs.x.ai/pricing")


def _spec(declaration: dict, include: tuple[str, ...] = (), price=None) -> HostedToolSpec:
    return HostedToolSpec(declaration_json=json.dumps(declaration), price=price, include=include)


SPECS = {
    ("xai", "openai_responses", "web_search"): _spec({"type": "web_search"}, price=SEARCH_PRICE),
    ("xai", "openai_responses", "x_search"): _spec({"type": "x_search"}),
    ("openai", "openai_responses", "web_search"): _spec({"type": "web_search"}, ("web_search_call.action.sources",)),
    ("anthropic", "anthropic", "web_search"): _spec({"type": "web_search_20250305", "name": "web_search"}),
    ("google", "google", "web_search"): _spec({"type": "google_search"}),
}
EXECUTOR_MODELS = {"xai": "grok-fast", "openai": "gpt-mini", "anthropic": "claude-haiku"}


def _image(model_id: str, description: str, *, price: float | None = None) -> ImageModelSpec:
    prices = (
        (ImageModelPrice(HostedToolPrice("image", price * 1000, date(2026, 10, 15), "https://docs.x.ai/pricing")),)
        if price is not None
        else ()
    )
    return ImageModelSpec(model_id, description, "images", ("images", "size"), ("pixels",), prices)


IMAGE_MODELS = {
    "openai": (_image("gpt-image-2", "OpenAI's image model"),),
    "xai": (_image("grok-imagine-image", "Grok Imagine", price=0.02),),
}


@pytest.fixture(autouse=True)
def _overlay(monkeypatch):
    monkeypatch.setattr(hosted_tools_module, "provider_executor_model", EXECUTOR_MODELS.get)
    monkeypatch.setattr(hosted_tools_module, "provider_image_models", lambda vendor: IMAGE_MODELS.get(vendor, ()))


def _spec_for(vendor, protocol, model_id, tool):
    return SPECS.get((vendor, protocol, tool))


def _source(source_id, vendor, protocol, model_id, *, kind="api_key", base_url=None) -> ModelHubSourceConfig:
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
        base_url=base_url,
    )


XAI = _source("src_xai00001", "xai", "openai_responses", "grok-4.6")
OPENAI = _source("src_oai00001", "openai", "openai_responses", "gpt-6-astra", base_url="https://relay.example/v1")
CLAUDE_SUB = _source("src_cla00001", "anthropic", "anthropic", "claude-opus-5-5", kind="subscription")
GEMINI = _source("src_gem00001", "google", "google", "gemini-3-pro")
ALL = [CLAUDE_SUB, XAI, OPENAI, GEMINI]


def _config(tools: dict[str, ModelHubHostedToolConfig] | None = None, sources=ALL) -> ModelHubConfig:
    vibey = ModelHubAgentSupplyConfig.default("vibey", mode="hub")
    vibey = replace(vibey, sources=replace(vibey.sources, order=[source.id for source in sources]))
    config = ModelHubConfig(sources=list(sources), hosted_tools=tools or {})
    config.agents["vibey"] = vibey
    return config


def _evidence(tmp_path, *refused) -> HostedEvidence:
    """Evidence holding a current refusal for each (source, model_id, tool, aspect)."""

    store = CapabilityEvidenceStore(tmp_path / "evidence.json")
    evidence = HostedEvidence(store, ENGINE, AVIBE)
    for source, model_id, tool, aspect in refused:
        store.write_refusal(
            EvidenceRecord(
                source_id=source.id, model_id=model_id, feature=EvidenceFeature.hosted(tool, aspect),
                status="unsupported", observed_at=NOW, wire=evidence.wire_for(source, model_id),
                source_identity=identity_digest(source), detail="400 invalid_request_error",
            ),
            live=ALL,
        )
    return evidence


@pytest.mark.parametrize(
    "primary,tool,refused,native,replay",
    [
        # The overlay offers it and nothing refused it: assumed, with no evidence at all.
        (XAI, "web_search", (), "assumed", "assumed"),
        (XAI, "web_search", ((XAI, "grok-4.6", "web_search", "replay"),), "assumed", "unsupported"),
        (XAI, "web_search", ((XAI, "grok-4.6", "web_search", "call"),), "unsupported", "unsupported"),
        # OpenAI offers no X search: the function, whatever the evidence.
        (OPENAI, "x_search", (), "unsupported", "unsupported"),
        (CLAUDE_SUB, "web_search", (), "unsupported", "unsupported"),
        (GEMINI, "web_search", (), "unsupported", "unsupported"),
    ],
    ids=["assumed", "replay-refused", "call-refused", "not-offered", "claude-subscription-never", "translated-never"],
)
def test_a_hops_facts_come_from_priors_and_refusals(tmp_path, primary, tool, refused, native, replay):
    """MH-HOSTED-001: nothing is probed; a tool is assumed where offered until a real request refuses it."""
    facts = hosted_tool_facts(
        primary, primary.models[0].id, tool, evidence=_evidence(tmp_path, *refused), spec_for=_spec_for,
    )
    assert (facts.native, facts.replay) == (native, replay)
    assert (facts.declaration is not None) is (native == "assumed")


def test_the_executor_chain_follows_the_users_order_then_vibeys(tmp_path):
    """MH-HOSTED-001: Sources whose vendor offers the tool, each on its vendor's executor model; a refused pair and
    a Claude subscription never are candidates."""
    evidence = _evidence(tmp_path)
    chain = executor_candidates(_config(), "web_search", evidence=evidence, spec_for=_spec_for)
    assert [(candidate.source.id, candidate.model_id) for candidate in chain] == [
        ("src_xai00001", "grok-fast"), ("src_oai00001", "gpt-mini"),
    ]
    ordered = _config({"web_search": ModelHubHostedToolConfig(executor_order=("src_oai00001", "src_gone00001"))})
    assert [candidate.source.id for candidate in executor_candidates(
        ordered, "web_search", evidence=evidence, spec_for=_spec_for,
    )] == ["src_oai00001", "src_xai00001"]
    refused = _evidence(tmp_path / "r", (XAI, "grok-fast", "web_search", "call"))
    assert [candidate.source.id for candidate in executor_candidates(
        _config(), "web_search", evidence=refused, spec_for=_spec_for,
    )] == ["src_oai00001"]


def test_the_image_enum_is_deterministic_with_the_default_first(tmp_path):
    """MH-HOSTED-001: a pure function of config and current refusals: the default first, the rest by id."""
    evidence = _evidence(tmp_path)
    choices = image_choices(_config(), evidence=evidence)
    assert [choice.model_id for choice in choices.models] == ["grok-imagine-image", "gpt-image-2"]
    saved = _config({"image_generate": ModelHubHostedToolConfig(
        default_image=ModelHubImageModelChoice("src_oai00001", "gpt-image-2"),
    )})
    choices = image_choices(saved, evidence=evidence)
    assert [choice.model_id for choice in choices.models] == ["gpt-image-2", "grok-imagine-image"]
    assert choices.saved_default_available is True
    refused = _evidence(tmp_path / "r", (OPENAI, "gpt-image-2", "image_generate", "call"))
    choices = image_choices(saved, evidence=refused)
    assert [choice.model_id for choice in choices.models] == ["grok-imagine-image"]
    assert (choices.saved_default_available, choices.default.model_id) == (False, "grok-imagine-image")
    assert image_choices(_config(sources=[CLAUDE_SUB, GEMINI]), evidence=evidence).models == ()


def test_resolution_states_v2_facts_in_todays_hop_shape(tmp_path):
    """MH-HOSTED-001: every tool is on unless turned off; native when assumed; the first executor binds."""
    resolved = resolve_hosted_tools(
        _config(), source=XAI, model_id="grok-4.6", evidence=_evidence(tmp_path), spec_for=_spec_for,
    )
    assert resolved == {
        "web_search": {
            "mode": "native", "declaration": {"type": "web_search"}, "include": [],
            "executor": {"source_id": "src_xai00001", "model_id": "grok-fast"}, "replay": True,
        },
        "x_search": {
            "mode": "native", "declaration": {"type": "x_search"}, "include": [],
            "executor": {"source_id": "src_xai00001", "model_id": "grok-fast"}, "replay": True,
        },
    }
    off = _config({"x_search": ModelHubHostedToolConfig(enabled=False)})
    assert set(resolve_hosted_tools(
        off, source=XAI, model_id="grok-4.6", evidence=_evidence(tmp_path / "o"), spec_for=_spec_for,
    )) == {"web_search"}


def test_the_tools_read_states_saved_and_effective_values_and_what_real_use_showed(tmp_path):
    """MH-HOSTED-004: the read never resolves a saved value, and its status comes from real requests only."""
    evidence = _evidence(tmp_path, (OPENAI, "gpt-6-astra", "web_search", "call"))
    evidence.store.note_success(
        EvidenceRecord(
            source_id=XAI.id, model_id="grok-fast", feature=EvidenceFeature.hosted("web_search", "call"),
            status="verified", observed_at=NOW, wire=evidence.wire_for(XAI, "grok-fast"),
            source_identity=identity_digest(XAI), mode="executor",
        ),
        live=ALL, now=NOW,
    )
    config = _config({"web_search": ModelHubHostedToolConfig(executor_order=("src_oai00001",))})
    status = hosted_tools_status(config, evidence=evidence, spec_for=_spec_for, image_price=lambda _source, _spec: None)
    by_name = {tool["name"]: tool for tool in status["tools"]}
    assert list(by_name) == ["web_search", "x_search", "image_generate"]
    web = by_name["web_search"]
    assert web["enabled"] is True and by_name["x_search"]["enabled"] is True
    assert web["executor_order"] == ["src_oai00001"]
    assert web["effective_order"] == ["src_oai00001", "src_xai00001"]
    assert [(c["source_id"], c["model_id"], c["relay"]) for c in web["executor_candidates"]] == [
        ("src_oai00001", "gpt-mini", True), ("src_xai00001", "grok-fast", False),
    ]
    assert web["executor_candidates"][1]["price"]["unit"] == "call"
    assert web["status"]["latest_ok"]["mode"] == "executor"
    assert [(item["source_id"], item["aspect"]) for item in web["status"]["refused"]] == [("src_oai00001", "call")]
    assert web["status"]["verified"] == [
        {"source_id": "src_xai00001", "model_id": "grok-fast", "mode": "executor", "at": "2026-10-15T12:00:00Z"}
    ]
    image = by_name["image_generate"]
    assert (image["default_model"], image["effective_default"]["model_id"]) == (None, "grok-imagine-image")
    assert image["image_offered_by"] == ["openai", "xai"]

    # The same Source id under a new credential is another Source: what the old one showed is not claimed for it.
    rotated = replace(XAI, credential_ref="cred_rotated")
    status = hosted_tools_status(
        _config(sources=[CLAUDE_SUB, rotated, OPENAI, GEMINI]), evidence=evidence, spec_for=_spec_for,
        image_price=lambda _source, _spec: None,
    )
    web = next(tool for tool in status["tools"] if tool["name"] == "web_search")
    assert (web["status"]["latest_ok"], web["status"]["verified"]) == (None, [])


def test_the_latest_success_is_the_newest_in_time_not_in_text(tmp_path):
    """MH-HOSTED-004: a time with no fraction of a second sorts after a later one with one when read as text."""
    from datetime import timedelta

    evidence = _evidence(tmp_path)
    for source, model_id, at in ((XAI, "grok-fast", NOW), (OPENAI, "gpt-mini", NOW + timedelta(milliseconds=500))):
        evidence.store.note_success(
            EvidenceRecord(
                source_id=source.id, model_id=model_id, feature=EvidenceFeature.hosted("web_search", "call"),
                status="verified", observed_at=at, wire=evidence.wire_for(source, model_id),
                source_identity=identity_digest(source), mode="executor",
            ),
            live=ALL, now=at,
        )
    status = hosted_tools_status(_config(), evidence=evidence, spec_for=_spec_for, image_price=lambda _s, _m: None)
    web = next(tool for tool in status["tools"] if tool["name"] == "web_search")
    assert web["status"]["latest_ok"]["source_id"] == "src_oai00001"


@pytest.mark.parametrize(
    "tool,payload,expected",
    [
        ("web_search", {"enabled": False}, {"enabled": False, "executor_order": ("src_oai00001",)}),
        ("web_search", {"executor_order": None}, {"enabled": True, "executor_order": None}),
        ("web_search", {"executor_order": ["src_xai00001"]}, {"enabled": True, "executor_order": ("src_xai00001",)}),
    ],
)
def test_a_put_changes_only_the_fields_it_names(tool, payload, expected):
    """MH-HOSTED-004: an absent field is unchanged and null resets; a toggle never drops a saved order."""
    config = _config({"web_search": ModelHubHostedToolConfig(executor_order=("src_oai00001",))})
    setting = apply_hosted_tool_put(config, tool, payload)
    assert (setting.enabled, setting.executor_order) == (expected["enabled"], expected["executor_order"])


@pytest.mark.parametrize(
    "tool,payload,code",
    [
        ("image_generation", {"enabled": True}, "hosted_tool_not_found"),
        ("web_search", {}, "invalid_parameter"),
        ("web_search", {"default_model": None}, "invalid_parameter"),
        ("image_generate", {"executor_order": None}, "invalid_parameter"),
        ("web_search", {"enabled": "yes"}, "invalid_parameter"),
        # Only the saved choices reset with null; a null switch must not turn a cost-bearing tool back on.
        ("web_search", {"enabled": None}, "invalid_parameter"),
        ("web_search", {"executor_order": ["not-a-source"]}, "invalid_parameter"),
        ("image_generate", {"default_model": {"source_id": "openai", "model_id": "gpt-image-2"}}, "invalid_parameter"),
        # A padded id would be saved and then never match the catalog id it names.
        ("image_generate", {"default_model": {"source_id": "src_oai00001", "model_id": " gpt-image-2 "}}, "invalid_parameter"),
        ("web_search", {"executor_order": ["src_a00000001", "src_a00000001"]}, "invalid_parameter"),
    ],
)
def test_a_put_refuses_what_the_contract_forbids(tool, payload, code):
    with pytest.raises(HostedToolSettingRefused) as refused:
        apply_hosted_tool_put(_config(), tool, payload)
    assert refused.value.code == code
