"""V2 ``model_hub.hosted_tools`` (``vibey-hosted-tools.md`` §3.3, §12).

Hosted tools are an optional feature inside the Model Hub section. Absence
means every tool is on, an rc-era section keeps each tool's ``enabled``, and a
broken entry takes only its own defaults: the rest of ``model_hub`` (Sources,
Agents) has to load untouched, never through the whole-section recovery reset.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from config.v2_config import (
    ModelHubConfig,
    ModelHubHostedToolConfig,
    ModelHubImageModelChoice,
    V2Config,
)
from core.handlers.model_hub.capability_evidence import EvidenceFeature
from core.handlers.model_hub.hosted_tools import enabled_tools
from core.services.settings import default_config
from vibe import api

FIXTURES = Path(__file__).parent / "fixtures" / "model_hub"
SOURCE = {
    "id": "src_xai00001",
    "kind": "api_key",
    "vendor": "xai",
    "display_name": "xAI 密钥",
    "protocol": "openai_responses",
    "supply_channel": "hub",
    "billing": "metered",
    "state": {"status": "standby", "retry_at": None, "detail_key": None},
    # An executor runs only on a model its Source lists, so the fixture lists xAI's lightweight model as xAI does.
    "models": [
        {"id": "grok-4.6", "display_name": "Grok 4.6", "origin": "discovered", "reasoning_efforts": [], "reasoning_efforts_source": None},
        {"id": "grok-4.20-0309-non-reasoning", "origin": "discovered", "reasoning_efforts": [], "reasoning_efforts_source": None},
    ],
    "created_at": "2026-10-13T00:00:00Z",
    "credential_ref": "cred_src_xai00001",
}
RELEASED_TOOLS = ("web_search", "x_search", "image_generate")
KNOWN_TOOLS = (*RELEASED_TOOLS, "web_fetch")


def _section(hosted_tools) -> dict:
    section = ModelHubConfig.from_payload({"sources": [SOURCE]}).to_payload()
    if hosted_tools is not ...:
        section["hosted_tools"] = hosted_tools
    return section


def test_released_configs_load_with_every_tool_on_and_stay_without_the_section():
    """MH-HOSTED-002: an upgrade from 3.2.0 turns every tool on without writing a section (owner decision)."""
    section = json.loads((FIXTURES / "released_pre_vibey_consumer.json").read_text(encoding="utf-8"))
    config = ModelHubConfig.from_payload(section, repairing=True)
    assert config.hosted_tools == {}
    assert enabled_tools(config) == KNOWN_TOOLS
    assert "hosted_tools" not in config.to_payload()


def test_hosted_tool_settings_round_trip_in_canonical_order():
    tools = {
        "image_generate": {"enabled": True, "default_model": {"source_id": "src_xai00001", "model_id": "grok-imagine-image"}},
        "x_search": {"enabled": False},
        "web_search": {"enabled": True, "executor_order": ["src_gone0001", "src_xai00001"]},
    }
    config = ModelHubConfig.from_payload(_section(tools))
    assert config.hosted_tools == {
        "web_search": ModelHubHostedToolConfig(True, ("src_gone0001", "src_xai00001")),
        "x_search": ModelHubHostedToolConfig(False),
        "image_generate": ModelHubHostedToolConfig(
            True, default_image=ModelHubImageModelChoice("src_xai00001", "grok-imagine-image"),
        ),
    }
    assert list(config.to_payload()["hosted_tools"]) == list(RELEASED_TOOLS)
    assert ModelHubConfig.from_payload(config.to_payload()).to_payload() == config.to_payload()


# (case, raw section, tools that survive as (enabled, executor_order, default_model), whether a warning is logged)
OFF = (False, None, None)
DEGRADED = [
    # Damage never turns a cost-bearing tool on: a broken section or entry is off (AGENTS persisted-shape rule).
    ("not-an-object", ["web_search"], dict.fromkeys(KNOWN_TOOLS, OFF), True),
    # Present but null is damage, not absence.
    ("null-section", None, dict.fromkeys(KNOWN_TOOLS, OFF), True),
    # An rc build's section: its keys and fields are unknown to v2, but each tool's opt-out survives.
    (
        "rc-era-section",
        {
            "web_search": {"enabled": False, "executor": {"source_id": "src_xai00001", "model_id": "grok-4.6"}},
            "image_generation": {"enabled": False, "executor": None},
        },
        {"web_search": OFF},
        True,
    ),
    ("unknown-tool", {"code_interpreter": {"enabled": False}, "x_search": {"enabled": False}}, {"x_search": OFF}, True),
    ("field-of-another-tool", {"image_generate": {"enabled": False, "executor_order": ["src_xai00001"]}}, {"image_generate": OFF}, True),
    ("non-boolean-enabled", {"web_search": {"enabled": "yes"}}, {"web_search": OFF}, True),
    ("repeated-order", {"web_search": {"executor_order": ["src_xai00001", "src_xai00001"]}}, {"web_search": OFF}, True),
    ("order-not-a-source-id", {"web_search": {"executor_order": [" src_xai00001"]}}, {"web_search": OFF}, True),
    ("bad-default-shape", {"image_generate": {"default_model": {"source_id": "src_xai00001"}}}, {"image_generate": OFF}, True),
    ("default-not-encodable", {"image_generate": {"default_model": {"source_id": "src_xai00001", "model_id": "x\ud800"}}}, {"image_generate": OFF}, True),
    # Saved values are never resolved: an order naming a Source that is gone is kept and skipped when read.
    ("order-source-gone", {"web_search": {"executor_order": ["src_gone0001"]}}, {"web_search": (True, ("src_gone0001",), None)}, False),
    ("entry-not-an-object", {"x_search": None}, {"x_search": OFF}, True),
    # The writer omits an unset field, so a present null is damage like any other bad value.
    ("null-order", {"web_search": {"executor_order": None}}, {"web_search": OFF}, True),
    ("null-default", {"image_generate": {"default_model": None}}, {"image_generate": OFF}, True),
    # Automatic is the order's absence; an empty list would read as a saved order that behaves as automatic.
    ("empty-order", {"web_search": {"executor_order": []}}, {"web_search": OFF}, True),
]


@pytest.mark.parametrize("raw,expected,warns", [case[1:] for case in DEGRADED], ids=[case[0] for case in DEGRADED])
def test_a_broken_hosted_tool_entry_turns_off_only_its_tool(raw, expected, warns, caplog):
    """MH-HOSTED-002: a broken entry turns its tool off and an unknown field is ignored; Sources and Agents load."""
    with caplog.at_level(logging.WARNING, logger="config.v2_config"):
        config = ModelHubConfig.from_payload(_section(raw))
    assert [source.id for source in config.sources] == ["src_xai00001"]
    assert {
        tool: (entry.enabled, entry.executor_order, entry.default_image) for tool, entry in config.hosted_tools.items()
    } == expected
    assert bool(caplog.records) == warns


def test_disk_load_with_a_broken_hosted_tools_section_keeps_model_hub(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["model_hub"] = _section({"web_search": {"enabled": 1}, "x_search": {"enabled": False}})
    config_path = tmp_path / "config.json"
    original = json.dumps(payload)
    config_path.write_text(original, encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert not loaded.load_warnings
    assert [source.id for source in loaded.model_hub.sources] == ["src_xai00001"]
    assert enabled_tools(loaded.model_hub) == ("image_generate", "web_fetch")
    assert config_path.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob("config.json.bak-recovery-*"))


def _validate_status(status):
    from jsonschema import Draft7Validator, RefResolver

    schema_dir = Path(__file__).parents[1] / "docs/plans/model-hub-contracts"
    schema = json.loads((schema_dir / "hosted-tools-status.schema.json").read_text())
    source_schema = json.loads((schema_dir / "source.schema.json").read_text())
    schema.pop("$id", None)
    source_schema.pop("$id", None)
    # The contract files use repository-relative ids; anchor this test resolver at
    # a synthetic absolute URI so jsonschema does not recursively prefix them.
    resolver = RefResolver(
        base_uri="https://schemas.avibe.test/model-hub/",
        referrer=schema,
        store={"https://schemas.avibe.test/model-hub/source.schema.json": source_schema},
    )
    Draft7Validator(schema, resolver=resolver).validate(status)


def _api_service(tmp_path):
    from tests.test_model_hub_api import _service

    service, store, _adapter = _service(tmp_path)
    store.config = ModelHubConfig.from_payload({"sources": [SOURCE]})
    return service, store


@pytest.mark.asyncio
async def test_the_tools_api_writes_one_field_of_one_tool_at_a_time(tmp_path):
    """MH-HOSTED-004: a write changes only the fields it names; nothing else in Model Hub changes."""
    from core.handlers.model_hub.service import ModelHubError

    service, store = _api_service(tmp_path)
    before = store.config.to_payload()

    status = service.hosted_tools_status()["hosted_tools"]
    _validate_status(status)
    by_name = {tool["name"]: tool for tool in status["tools"]}
    assert list(by_name) == list(KNOWN_TOOLS) and all(tool["enabled"] for tool in status["tools"])
    # The executor runs on the vendor's lightweight model, priced by the overlay's dated list price.
    (candidate,) = by_name["x_search"]["executor_candidates"]
    assert (candidate["source_id"], candidate["model_id"], candidate["billing"], candidate["relay"]) == (
        "src_xai00001", "grok-4.20-0309-non-reasoning", "metered", False,
    )
    assert candidate["price"] == {
        "unit": "post",
        "usd_per_1k": 5,
        "extra": [{"unit": "profile", "usd_per_1k": 10}],
        "as_of": "2026-10-13",
        "url": "https://docs.x.ai/developers/pricing",
    }

    await service.set_hosted_tool("x_search", {"executor_order": ["src_xai00001"]})
    written = await service.set_hosted_tool("x_search", {"enabled": False})
    after = store.config.to_payload()
    assert after.pop("hosted_tools") == {"x_search": {"enabled": False, "executor_order": ["src_xai00001"]}}
    assert after == before
    _validate_status(written["hosted_tools"])
    await service.set_hosted_tool("x_search", {"executor_order": None})
    assert store.config.to_payload()["hosted_tools"] == {"x_search": {"enabled": False}}

    for tool, body, code, status_code in [
        ("image_generation", {"enabled": True}, "hosted_tool_not_found", 404),
        ("web_search", {}, "invalid_parameter", 400),
        ("web_search", {"default_model": None}, "invalid_parameter", 400),
        ("web_search", {"enabled": True, "executor": None}, "invalid_parameter", 400),
    ]:
        with pytest.raises(ModelHubError) as refused:
            await service.set_hosted_tool(tool, body)
        assert (refused.value.code, refused.value.status) == (code, status_code), (tool, body)
    assert store.config.to_payload()["hosted_tools"] == {"x_search": {"enabled": False}}


def _refuse(service, source, model_id, feature):
    from core.handlers.model_hub.capability_evidence import EvidenceRecord, identity_digest

    evidence = service.hosted_evidence()
    service.capability_evidence.write_refusal(
        EvidenceRecord(
            source_id=source.id, model_id=model_id, feature=feature, status="unsupported",
            observed_at=service.now(), wire=evidence.wire_for(source, model_id),
            source_identity=identity_digest(source), detail="400 invalid_request_error",
        ),
        live=lambda: [source],
        wire_for=evidence.wire_for,
    )


@pytest.mark.asyncio
async def test_a_clear_removes_only_the_refusals_it_names(tmp_path):
    """MH-HOSTED-003: the user clears a refusal; it is gone at once and the next request tries the feature again."""
    from core.handlers.model_hub.service import ModelHubError

    service, store = _api_service(tmp_path)
    (source,) = store.config.sources
    executor = "grok-4.20-0309-non-reasoning"
    for model_id in ("grok-4.6", executor):
        _refuse(service, source, model_id, EvidenceFeature.hosted("x_search", "call"))
    _refuse(service, source, "grok-4.6", EvidenceFeature.hosted("web_search", "replay"))
    _refuse(service, source, "grok-4.6", EvidenceFeature.effort("max"))

    def refused(tool):
        status = service.hosted_tools_status()["hosted_tools"]
        tool = next(item for item in status["tools"] if item["name"] == tool)
        return sorted((item["model_id"], item["aspect"]) for item in tool["status"]["refused"])

    assert refused("x_search") == [("grok-4.20-0309-non-reasoning", "call"), ("grok-4.6", "call")]
    cleared = await service.clear_hosted_refusals("x_search", {"source_id": source.id, "model_id": executor})
    _validate_status(cleared["hosted_tools"])
    assert refused("x_search") == [("grok-4.6", "call")]
    await service.clear_hosted_refusals("x_search", {})
    assert refused("x_search") == [] and refused("web_search") == [("grok-4.6", "replay")]

    answer = await service.clear_source_evidence(source.id, {"model_id": "grok-4.6", "feature": {"kind": "effort", "name": "max"}})
    assert answer["source"]["id"] == source.id
    assert not service.hosted_evidence().unsupported(source, "grok-4.6", EvidenceFeature.effort("max"))
    assert refused("web_search") == [("grok-4.6", "replay")]

    for call, code, status_code in [
        (service.clear_hosted_refusals("image_generation", {}), "hosted_tool_not_found", 404),
        # Nothing is nullable: a malformed body is refused, never widened into a broader clear.
        (service.clear_hosted_refusals("x_search", {"source_id": None}), "invalid_parameter", 400),
        (service.clear_hosted_refusals("x_search", {"source_id": source.id, "model_id": None}), "invalid_parameter", 400),
        (service.clear_hosted_refusals("x_search", {"source_id": source.id, "model_id": " grok-4.6"}), "invalid_parameter", 400),
        (service.clear_source_evidence(source.id, {"model_id": "grok-4.6 ", "feature": {"kind": "effort", "name": "max"}}), "invalid_parameter", 400),
        # The feature is exactly the shape the evidence writes: an effort has no aspect, not even a null one.
        (service.clear_source_evidence(source.id, {"model_id": "grok-4.6", "feature": {"kind": "effort", "name": "max", "aspect": None}}), "invalid_parameter", 400),
        (service.clear_hosted_refusals("x_search", {"model_id": executor}), "invalid_parameter", 400),
        (service.clear_hosted_refusals("x_search", {"source_id": ""}), "invalid_parameter", 400),
        (service.clear_source_evidence("src_gone0001", {"model_id": "grok-4.6", "feature": {"kind": "effort", "name": "max"}}), "source_not_found", 404),
        (service.clear_source_evidence(source.id, {"model_id": "grok-4.6", "feature": {"kind": "hosted_tool", "name": "x_search", "aspect": "call"}}), "invalid_parameter", 400),
    ]:
        with pytest.raises(ModelHubError) as refusal:
            await call
        assert (refusal.value.code, refusal.value.status) == (code, status_code)


@pytest.mark.asyncio
async def test_a_clear_that_cannot_be_saved_changes_nothing(tmp_path, monkeypatch):
    """MH-HOSTED-003: a failed write answers 500 and the refusal still holds, so nothing reappears after a restart."""
    from core.handlers.model_hub import capability_evidence
    from core.handlers.model_hub.service import ModelHubError

    service, store = _api_service(tmp_path)
    (source,) = store.config.sources
    feature = EvidenceFeature.hosted("x_search", "call")
    _refuse(service, source, "grok-4.6", feature)

    def full_disk(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(capability_evidence, "write_state_document", full_disk)
    with pytest.raises(ModelHubError) as refusal:
        await service.clear_hosted_refusals("x_search", {})
    assert (refusal.value.code, refusal.value.status) == ("state_write_failed", 500)
    assert service.hosted_evidence().unsupported(source, "grok-4.6", feature)


@pytest.mark.asyncio
async def test_a_clear_waits_for_a_source_change_in_flight(tmp_path):
    """MH-HOSTED-003: a clear runs after a concurrent Source change commits, so it keeps evidence under the identity
    the config now holds and answers the Source as it now is."""
    import asyncio
    from dataclasses import replace

    service, store = _api_service(tmp_path)
    (source,) = store.config.sources
    _refuse(service, source, "grok-4.6", EvidenceFeature.effort("max"))
    body = {"model_id": "grok-4.6", "feature": {"kind": "effort", "name": "max"}}

    async with service._mutation_lock:
        clearing = asyncio.create_task(service.clear_source_evidence(source.id, body))
        await asyncio.sleep(0.05)
        assert not clearing.done()
        store.config.sources = [replace(source, display_name="xAI 工作密钥")]
    answer = await clearing
    assert answer["source"]["display_name"] == "xAI 工作密钥"


@pytest.mark.parametrize(
    "prices,expected",
    [
        ((), None),
        # A price for one size or quality is not every image's: the card shows no unit price rather than a wrong one.
        ((("1024x1024", "low", 11),), None),
        ((("1024x1024", "low", 11), (None, None, 20)), {"usd": 0.02, "basis": "list"}),
    ],
    ids=["unpriced", "conditional-only", "unqualified"],
)
def test_an_image_models_card_price_is_only_an_unqualified_list_price(tmp_path, monkeypatch, prices, expected):
    """MH-HOSTED-004: gpt-image-2's prices vary by size and quality, so its card reads "no unit price" (owner rule)."""
    from datetime import date

    from core.handlers.model_hub import hosted_tools as hosted_tools_module
    from vibe.backend_model_catalog import HostedToolPrice, ImageModelPrice, ImageModelSpec

    spec = ImageModelSpec(
        "gpt-image-2", "OpenAI image model", ("images", "size"), ("pixels",),
        prices=tuple(
            ImageModelPrice(HostedToolPrice("image", usd, date(2026, 10, 15), "https://openai.com/api/pricing"), size, quality)
            for size, quality, usd in prices
        ),
    )
    monkeypatch.setattr(
        hosted_tools_module, "source_image_models", lambda vendor, protocol, listed: (spec,) if vendor == "xai" else (),
    )
    service, _store = _api_service(tmp_path)
    status = service.hosted_tools_status()["hosted_tools"]
    _validate_status(status)
    image = next(tool for tool in status["tools"] if tool["name"] == "image_generate")
    assert [model["price"] for model in image["models"]] == [expected]


@pytest.mark.parametrize("valued,expected", [(0.0, None), (0.067, {"usd": 0.067, "basis": "estimated"})])
def test_an_estimated_image_price_of_zero_is_no_unit_price(tmp_path, monkeypatch, valued, expected):
    """MH-HOSTED-004: an estimate the pricing owner values at zero means the rate is unknown, never a free image."""
    from types import SimpleNamespace

    from core.handlers.model_hub import hosted_tools as hosted_tools_module
    from vibe.backend_model_catalog import ImageModelSpec

    spec = ImageModelSpec("gemini-3-image", "Nano Banana", ("images",), (), tokens_per_image=1290)
    monkeypatch.setattr(
        hosted_tools_module, "source_image_models", lambda vendor, protocol, listed: (spec,) if vendor == "xai" else (),
    )
    service, _store = _api_service(tmp_path)
    monkeypatch.setattr(service, "hosted_call_cost", lambda *_args, **_kwargs: SimpleNamespace(usd=valued))
    image = next(tool for tool in service.hosted_tools_status()["hosted_tools"]["tools"] if tool["name"] == "image_generate")
    assert [model["price"] for model in image["models"]] == [expected]
