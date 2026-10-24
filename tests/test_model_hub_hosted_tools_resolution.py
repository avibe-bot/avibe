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
    KEYLESS_EXA_SOURCE_ID,
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
    freeze_turn_tools,
    hop_hosted_tools,
    hosted_tool_facts,
    hosted_tools_status,
    image_choices,
)
from core.handlers.model_hub.hosted_turn import ExecutorSlot
from vibe.backend_model_catalog import HostedToolPrice, HostedToolSpec, ImageModelPrice, ImageModelSpec, hosted_tool_spec

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
EXECUTOR_MODELS = {"xai": ("grok-fast",), "openai": ("gpt-mini",), "anthropic": ("claude-haiku",)}


def _image(model_id: str, description: str, *, price: float | None = None, kinds=("api_key", "subscription")) -> ImageModelSpec:
    prices = (
        (ImageModelPrice(HostedToolPrice("image", price * 1000, date(2026, 10, 15), "https://docs.x.ai/pricing")),)
        if price is not None
        else ()
    )
    return ImageModelSpec(model_id, description, ("images", "size"), ("pixels",), kinds=kinds, prices=prices)


IMAGE_MODELS = {
    # An image model verified on API keys only: a subscription that lists it is not offered it.
    "openai": (_image("gpt-image-2", "OpenAI's image model", kinds=("api_key",)),),
    "xai": (_image("grok-imagine-image", "Grok Imagine", price=0.02),),
}


@pytest.fixture(autouse=True)
def _overlay(monkeypatch, request):
    monkeypatch.setattr(
        hosted_tools_module, "source_executor_models", lambda vendor, protocol: EXECUTOR_MODELS.get(vendor, ()),
    )
    monkeypatch.setattr(hosted_tools_module, "provider_image_models", lambda vendor: IMAGE_MODELS.get(vendor, ()))
    monkeypatch.setattr(
        hosted_tools_module, "source_image_models", lambda vendor, protocol, listed: IMAGE_MODELS.get(vendor, ()),
    )
    # These cases pin model-executor priors and ordering. The active keyless Exa
    # chain is covered by the owning search-chain tests; omit that catalog service
    # from this model-only fixture without changing the shipped activation predicate.
    original_service = hosted_tools_module.model_hub_search_service

    def without_keyless(source):
        if source.id == KEYLESS_EXA_SOURCE_ID:
            return None
        return original_service(source)

    hosted_tools_module.model_hub_search_service = without_keyless
    request.addfinalizer(lambda: setattr(hosted_tools_module, "model_hub_search_service", original_service))
    # The bundled overlay's vendors; which of them list image models is `IMAGE_MODELS`.
    monkeypatch.setattr(hosted_tools_module, "bundled_provider_vendors", lambda: dict.fromkeys(("anthropic", "openai", "xai")))


def _spec_for(vendor, protocol, model_id, tool):
    return SPECS.get((vendor, protocol, tool))


def _source(source_id, vendor, protocol, model_id, *, kind="api_key", base_url=None, listed=()) -> ModelHubSourceConfig:
    return ModelHubSourceConfig(
        id=source_id,
        kind=kind,
        vendor=vendor,
        display_name=source_id,
        protocol=protocol,
        supply_channel="hub",
        billing="monthly" if kind == "subscription" else "metered",
        state=ModelHubSourceStateConfig(status="standby"),
        models=[ModelHubModelConfig(id=model, provenance="discovered") for model in (model_id, *listed)],
        credential_ref=f"cred_{source_id}",
        base_url=base_url,
    )


XAI = _source("src_xai00001", "xai", "openai_responses", "grok-4.6", listed=("grok-fast",))
OPENAI = _source(
    "src_oai00001", "openai", "openai_responses", "gpt-6-astra", base_url="https://relay.example/v1", listed=("gpt-mini",),
)
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
            live=lambda: ALL,
            wire_for=evidence.wire_for,
        )
    return evidence


@pytest.mark.parametrize(
    "primary,tool,refused,native,replay,fallback",
    [
        # The overlay offers it and nothing refused it: assumed, with no evidence at all.
        (XAI, "web_search", (), "assumed", "assumed", False),
        (XAI, "web_search", ((XAI, "grok-4.6", "web_search", "replay"),), "assumed", "unsupported", False),
        # This Source refused it: a proxied call falls back from it.
        (XAI, "web_search", ((XAI, "grok-4.6", "web_search", "call"),), "unsupported", "unsupported", True),
        # OpenAI offers no X search: the function, whatever the evidence, and never a fallback.
        (OPENAI, "x_search", (), "unsupported", "unsupported", False),
        (OPENAI, "x_search", ((OPENAI, "gpt-6-astra", "x_search", "call"),), "unsupported", "unsupported", False),
        (CLAUDE_SUB, "web_search", (), "unsupported", "unsupported", False),
        (GEMINI, "web_search", (), "unsupported", "unsupported", False),
    ],
    ids=[
        "assumed", "replay-refused", "call-refused", "not-offered", "not-offered-refused",
        "claude-subscription-never", "translated-never",
    ],
)
def test_a_hops_facts_come_from_priors_and_refusals(tmp_path, primary, tool, refused, native, replay, fallback):
    """MH-HOSTED-001: nothing is probed; a tool is assumed where offered until a real request refuses it, and only
    that refusal marks the hop as the one a proxied call falls back from."""
    facts = hosted_tool_facts(
        primary, primary.models[0].id, tool, evidence=_evidence(tmp_path, *refused), spec_for=_spec_for,
    )
    assert (facts.native, facts.replay, facts.refused) == (native, replay, fallback)
    assert (facts.declaration is not None) is (native == "assumed")


@pytest.mark.parametrize(
    "listed,retired,expected",
    [
        # The relay that 404'd the catalog prior: the next listed model of the family runs the call.
        (("claude-3-5-haiku-20241022", "claude-haiku-4-5", "claude-haiku-4-5-20251001"), (), "claude-haiku-4-5"),
        (("claude-haiku-5-5", "claude-haiku-4-5"), (), "claude-haiku-5-5"),
        # A dated snapshot names its model, the latest first; a lookalike id does not.
        (("claude-haiku-4-5-20251001", "claude-haiku-4-5-20260101"), (), "claude-haiku-4-5-20260101"),
        (("claude-haiku-5-50",), (), None),
        # A retired row is not listed; a Source that lists none runs no executor call.
        (("claude-haiku-5-5", "claude-haiku-4-5"), ("claude-haiku-5-5",), "claude-haiku-4-5"),
        (("claude-opus-5-5",), (), None),
    ],
    ids=["prior-unlisted", "prior-listed", "dated-snapshot", "lookalike", "retired", "none-listed"],
)
def test_an_executor_runs_on_a_model_its_source_lists(tmp_path, monkeypatch, listed, retired, expected):
    """MH-HOSTED-001: an API-key Source admits unlisted models on a hop, but an executor call nobody asked for must
    not guess one: the first of the vendor's executor models the Source lists, or the Source is no executor."""
    monkeypatch.setattr(
        hosted_tools_module, "source_executor_models",
        lambda vendor, protocol: ("claude-haiku-5-5", "claude-haiku-4-5") if vendor == "anthropic" else (),
    )
    source = _source("src_rel00001", "anthropic", "anthropic", listed[0], listed=listed[1:])
    for model in source.models:
        model.retired = model.id in retired or None
    chain = executor_candidates(_config(sources=[source]), "web_search", evidence=_evidence(tmp_path), spec_for=_spec_for)
    assert [candidate.model_id for candidate in chain] == ([] if expected is None else [expected])


RELAY_MODELS = ("gpt-6-luna", "grok-4.20-0309-non-reasoning", "claude-haiku-5-5", "gpt-image-2", "gemini-2.5-flash-image")


@pytest.mark.parametrize(
    ("protocol", "listed", "native", "executors", "images"),
    [
        # The reported relay (#2873): OpenAI's models on OpenAI's protocol run what an OpenAI key would.
        ("openai_responses", ("gpt-6-luna", "gpt-image-2"), {"gpt-6-luna": {"web_search"}},
         {"web_search": "gpt-6-luna"}, ["gpt-image-2"]),
        # One relay serving several vendors: each model takes its own vendor's priors, and each tool runs on an
        # executor model that runs it, not merely the first one listed.
        ("openai_responses", RELAY_MODELS,
         {"gpt-6-luna": {"web_search"}, "grok-4.20-0309-non-reasoning": {"web_search", "x_search"},
          "claude-haiku-5-5": set()},
         {"web_search": "gpt-6-luna", "x_search": "grok-4.20-0309-non-reasoning"}, ["gpt-image-2"]),
        # Anthropic's protocol: Claude's web search, natively and as executor; it has no image endpoint.
        ("anthropic", RELAY_MODELS, {"claude-haiku-5-5": {"web_search"}, "gpt-6-luna": set()},
         {"web_search": "claude-haiku-5-5"}, []),
        # Chat Completions declares no search; Gemini's image models are its vendor's Chat Completions ones.
        ("openai_chat", RELAY_MODELS, {"gpt-6-luna": set()}, {}, ["gemini-2.5-flash-image"]),
        # No executor model listed and no image model listed: the vendor's are not guessed, unlike a hop's model.
        # A model no vendor claims takes no priors at all.
        ("openai_responses", ("gpt-6-astra", "deepseek-chat"), {"gpt-6-astra": {"web_search"}, "deepseek-chat": set()},
         {}, []),
    ],
    ids=["reported-relay", "multi-vendor-responses", "multi-vendor-anthropic", "multi-vendor-chat", "unlisted"],
)
def test_a_custom_relays_models_take_the_priors_of_the_vendor_that_claims_each(
    tmp_path, monkeypatch, protocol, listed, native, executors, images,
):
    """MH-HOSTED-001: a custom Source names no vendor, so the bundled overlay's vendor that claims a model supplies
    its priors, on that vendor's protocol only, with executor and image models only from the Source's listing."""
    monkeypatch.undo()  # The bundled overlay, not this module's priors.
    relay = _source("src_rly00001", "custom", protocol, listed[0], base_url="https://relay.example/v1", listed=listed[1:])
    config, evidence = _config(sources=[relay]), _evidence(tmp_path)
    assert {
        model_id: {
            tool for tool in ("web_search", "x_search")
            if hosted_tool_facts(relay, model_id, tool, evidence=evidence, spec_for=hosted_tool_spec).native == "assumed"
        }
        for model_id in native
    } == native
    chains = {
        tool: executor_candidates(config, tool, evidence=evidence, spec_for=hosted_tool_spec)
        for tool in ("web_search", "x_search")
    }
    assert {tool: chain[0].model_id for tool, chain in chains.items() if chain} == executors
    assert [choice.model_id for choice in image_choices(config, evidence=evidence).models] == images


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
    codex_sub = _source("src_osub0001", "openai", "openai_responses", "gpt-image-2", kind="subscription")
    assert image_choices(_config(sources=[codex_sub]), evidence=evidence).models == ()
    # An OpenAI key on a protocol whose engine entry has no images endpoints is never offered one.
    on_messages = _source("src_oanth001", "openai", "anthropic", "gpt-6-astra")
    assert image_choices(_config(sources=[on_messages]), evidence=evidence).models == ()


def test_a_turn_freezes_its_tools_executors_and_image_enum_and_each_hop_states_its_own_facts(tmp_path):
    """MH-HOSTED-001: every tool is on unless turned off; the turn freezes the set, each search tool's executor order
    with the identity it was built under, and the image enum; a hop states facts only, the enum's params sorted."""
    evidence = _evidence(tmp_path)
    turn = freeze_turn_tools(_config(), evidence=evidence, spec_for=_spec_for)
    assert turn.tools == {"web_search", "x_search", "image_generate"}
    assert turn.executors == {
        "web_search": (
            ExecutorSlot("src_xai00001", "grok-fast", identity_digest(XAI), label="src_xai00001"),
            ExecutorSlot("src_oai00001", "gpt-mini", identity_digest(OPENAI), label="src_oai00001"),
        ),
        "x_search": (ExecutorSlot("src_xai00001", "grok-fast", identity_digest(XAI)),),
    }
    image = {"description": "Grok Imagine", "params": ["images", "size"], "size_forms": ["pixels"]}
    assert hop_hosted_tools(turn, OPENAI, "gpt-6-astra", evidence=evidence, spec_for=_spec_for) == {
        "web_search": {
            "native": "assumed", "declaration": {"type": "web_search"},
            "include": ["web_search_call.action.sources"], "replay": "assumed",
        },
        # OpenAI does not run X search: the hop takes the function.
        "x_search": {"native": "unsupported", "replay": "unsupported"},
        "image_generate": {
            "models": [
                {"source_id": "src_xai00001", "model_id": "grok-imagine-image", **image},
                {**{"source_id": "src_oai00001", "model_id": "gpt-image-2", **image}, "description": "OpenAI's image model"},
            ],
            "default": {"source_id": "src_xai00001", "model_id": "grok-imagine-image"},
        },
    }
    # A hop whose Source refused a tool says so; the image enum stays the turn's.
    refused = _evidence(tmp_path / "r", (XAI, "grok-4.6", "web_search", "call"))
    assert hop_hosted_tools(turn, XAI, "grok-4.6", evidence=refused, spec_for=_spec_for)["web_search"] == {
        "native": "unsupported", "replay": "unsupported", "refused": True,
    }
    # Off, or with no image model available, a tool is not declared at all.
    off = _config({"x_search": ModelHubHostedToolConfig(enabled=False)}, sources=[CLAUDE_SUB, OPENAI, GEMINI])
    turn = freeze_turn_tools(
        off, evidence=_evidence(tmp_path / "o", (OPENAI, "gpt-image-2", "image_generate", "call")), spec_for=_spec_for,
    )
    assert (turn.tools, turn.images) == ({"web_search"}, ())
    assert set(hop_hosted_tools(turn, OPENAI, "gpt-6-astra", evidence=evidence, spec_for=_spec_for)) == {"web_search"}


def test_the_tools_read_states_saved_and_effective_values_and_what_real_use_showed(tmp_path):
    """MH-HOSTED-004: the read never resolves a saved value, and its status comes from real requests only."""
    evidence = _evidence(tmp_path, (OPENAI, "gpt-6-astra", "web_search", "call"))
    evidence.store.note_success(
        EvidenceRecord(
            source_id=XAI.id, model_id="grok-fast", feature=EvidenceFeature.hosted("web_search", "call"),
            status="verified", observed_at=NOW, wire=evidence.wire_for(XAI, "grok-fast"),
            source_identity=identity_digest(XAI), mode="executor",
        ),
        live=lambda: ALL, now=NOW,
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
    # Each model carries its serving Source's billing, as the executor rows do.
    assert [(m["source_id"], m["billing"]) for m in image["models"]] == [("src_xai00001", "metered"), ("src_oai00001", "metered")]
    assert image["image_offered_by"] == ["openai", "xai"]
    # Which vendors offer image models is a catalog fact: with no image vendor configured, the read still names
    # the vendors a key could add one from.
    bare = hosted_tools_status(
        _config(sources=[CLAUDE_SUB]), evidence=_evidence(tmp_path / "bare"), spec_for=_spec_for,
        image_price=lambda _source, _spec: None,
    )
    bare_image = next(tool for tool in bare["tools"] if tool["name"] == "image_generate")
    assert (bare_image["models"], bare_image["image_offered_by"]) == ([], ["openai", "xai"])

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
            live=lambda: ALL, now=at,
        )
    status = hosted_tools_status(_config(), evidence=evidence, spec_for=_spec_for, image_price=lambda _s, _m: None)
    web = next(tool for tool in status["tools"] if tool["name"] == "web_search")
    assert web["status"]["latest_ok"]["source_id"] == "src_oai00001"


def test_the_latest_success_is_the_newest_one_whose_source_is_still_the_same(tmp_path):
    """MH-HOSTED-004: once the newest success's Source changes identity, the latest is the newest other success, as
    fresh as memory holds it, not as old as the hourly file write."""
    from datetime import timedelta

    evidence = _evidence(tmp_path)

    def succeed(source, model_id, at):
        evidence.store.note_success(
            EvidenceRecord(
                source_id=source.id, model_id=model_id, feature=EvidenceFeature.hosted("web_search", "call"),
                status="verified", observed_at=at, wire=evidence.wire_for(source, model_id),
                source_identity=identity_digest(source), mode="executor",
            ),
            live=lambda: ALL, now=at,
        )

    succeed(XAI, "grok-fast", NOW)
    succeed(XAI, "grok-fast", NOW + timedelta(minutes=10))
    succeed(OPENAI, "gpt-mini", NOW + timedelta(minutes=20))
    rotated = replace(OPENAI, credential_ref="cred_rotated")
    status = hosted_tools_status(
        _config(sources=[CLAUDE_SUB, XAI, rotated, GEMINI]), evidence=evidence, spec_for=_spec_for,
        image_price=lambda _s, _m: None,
    )
    web = next(tool for tool in status["tools"] if tool["name"] == "web_search")
    assert web["status"]["latest_ok"] == {
        "source_id": "src_xai00001", "model_id": "grok-fast", "mode": "executor", "at": "2026-10-15T12:10:00Z",
    }


@pytest.mark.parametrize(
    "base_url,relay",
    [
        (None, False),
        ("https://api.openai.com/v1", False),
        # Spellings of the official endpoint the engine treats as the same one.
        ("https://API.OpenAI.com:443/v1/", False),
        ("https://api.openai.com", False),
        ("https://relay.example/v1", True),
        ("https://api.openai.com:8443/v1", True),
    ],
)
def test_a_relay_is_any_endpoint_but_the_vendors_own_by_the_engines_endpoint_identity(base_url, relay):
    """MH-HOSTED-004: an executor candidate is marked relay, whose bill Avibe cannot know, only off the official
    endpoint."""
    from core.handlers.model_hub.hosted_tools import is_relay

    assert is_relay(replace(OPENAI, base_url=base_url)) is relay


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
        ("web_search", {"executor_order": []}, "invalid_parameter"),
        ("image_generate", {"default_model": {"source_id": "openai", "model_id": "gpt-image-2"}}, "invalid_parameter"),
        # A padded id would be saved and then never match the catalog id it names.
        ("image_generate", {"default_model": {"source_id": "src_oai00001", "model_id": " gpt-image-2 "}}, "invalid_parameter"),
        # One admission rule for model ids: text UTF-8 storage and transport cannot encode is refused too.
        ("image_generate", {"default_model": {"source_id": "src_oai00001", "model_id": "gpt-image-\ud800"}}, "invalid_parameter"),
        ("web_search", {"executor_order": ["src_a00000001", "src_a00000001"]}, "invalid_parameter"),
    ],
)
def test_a_put_refuses_what_the_contract_forbids(tool, payload, code):
    with pytest.raises(HostedToolSettingRefused) as refused:
        apply_hosted_tool_put(_config(), tool, payload)
    assert refused.value.code == code
